# Deploy plan — staging & prod on k3s (OCI A1.Flex)

**Status: PLAN.** The live deployment today is the single-environment Podman/Quadlet setup described in [DEPLOY.md](DEPLOY.md). This document is the plan for moving to two environments — **staging** and **prod** — on the k3s cluster defined in the cluster-topology artifact:
<https://claude.ai/code/artifact/55c5f771-9585-4198-b5e8-2d9175ae8c6b>

## Target topology (what the artifact defines)

One OCI Ampere A1.Flex node (ARM64, 2 OCPU, 12 GB RAM, 200 GB boot volume) running single-node k3s:

- **Ingress**: bundled Traefik, host-based routing, TLS via cert-manager + Let's Encrypt.
- **Runtime**: containerd, running images built on the node with Podman — no registry.
- **Storage**: local-path provisioner on the boot volume.
- **App layout**: one namespace per app. This bot is the `chores` app — 1 replica, ~0.2 CPU, 256 Mi, outbound egress to `generativelanguage.googleapis.com` (Gemini).
- **Data plane**: one shared Postgres 17 StatefulSet in namespace `data` (1 CPU, 1–2 Gi), one database + one role per app (`chores` / `chores_rw`), each role confined to its own database.
- **Deploy flow**: per-app repo → build on node with Podman → load image into containerd → `kubectl apply -k` (Kustomize overlay). No registry at this scale; the artifact defers Flux/GitOps until the cluster grows. The artifact triggers builds via webhook; this plan keeps the repo's existing GitHub Actions → SSH trigger instead, which lands on the same build-on-node flow with no new secrets.

Two constraints from the artifact worth keeping in mind: **CPU is the binding resource** on this node (RAM is plentiful), and per-app Postgres connection pools stay small (5–10).

Three deltas between the artifact and this repo today:

1. **Database** — the artifact puts `chores` on shared Postgres; the bot currently uses SQLite (`aiosqlite`). The plan runs SQLite on a PVC per environment first, and treats the Postgres migration as a separate later phase (it requires a code change in `storage/`).
2. **Ingress** — the artifact routes `chores.example.com`, but the bot's only HTTP surface is an internal health/metrics port (8080: `/health`, `/ready`, `/metrics`); Discord is an outbound WebSocket and Gemini is an outbound API call. The port feeds the liveness/readiness probes and (later) Prometheus scraping — no Ingress or Service is created until the bot grows a user-facing web UI.
3. **Environments** — the artifact describes a single production cluster and defines no staging. The staging environment below is this plan's addition, carved out of the `chores` CPU allocation (see the node-budget note under Environments).

## Environments

| | Staging | Prod |
|---|---|---|
| Namespace | `chores-staging` | `chores` |
| Discord bot | **separate Discord application** + token, invited only to a test server | the real bot |
| Gemini key | separate API key (keeps quota/billing visible per env) | production key |
| Database | SQLite on PVC `snoopy-data` in `chores-staging` | SQLite on PVC `snoopy-data` in `chores` |
| Image tag | `localhost/snoopy-home:<git-sha>` , auto-deployed | the same `<git-sha>` tag, promoted — never rebuilt |
| Deploy trigger | every push to `main` (after tests) | manual promotion with approval |
| Resources | requests 50m / 128 Mi, limits 200m / 256 Mi | requests 100m / 128 Mi, limits 200m / 256 Mi |

Full separation is not optional for a Discord bot: two instances sharing one bot token answer every message twice, and two schedulers sharing one database fire every reminder twice. Staging must have its own token, its own test server, and its own database file.

Both environments fit the node budget: prod + staging together request 0.15 CPU (150m), under the ~0.2 the artifact allocates to `chores`, with limits summing to 0.4 CPU for burst — acceptable while the other apps are below their caps, and staging can be scaled to zero (`kubectl -n chores-staging scale deploy/chores --replicas=0`) whenever the node gets tight.

## Repo layout to add

```
deploy/k8s/
  base/
    kustomization.yaml
    deployment.yaml
    pvc.yaml
  overlays/
    staging/
      kustomization.yaml   # namespace: chores-staging, staging resource patch
    prod/
      kustomization.yaml   # namespace: chores, prod resource patch
```

Core of `base/deployment.yaml`:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: chores
spec:
  replicas: 1
  strategy:
    type: Recreate        # never two bots at once: duplicate replies + SQLite contention
  selector:
    matchLabels: { app: chores }
  template:
    metadata:
      labels: { app: chores }
    spec:
      containers:
        - name: chores
          image: localhost/snoopy-home:latest   # tag overridden per overlay
          imagePullPolicy: Never                # image is imported into containerd, never pulled
          envFrom:
            - secretRef: { name: chores-secrets }
          env:
            - { name: PYTHONUNBUFFERED, value: "1" }
            - { name: DB_PATH, value: /data/snoopy_home.db }
          volumeMounts:
            - { name: data, mountPath: /data }
          ports:
            - { name: http, containerPort: 8080 }
          livenessProbe:
            httpGet: { path: /health, port: http }
            initialDelaySeconds: 10
            periodSeconds: 15
          readinessProbe:
            httpGet: { path: /ready, port: http }
            initialDelaySeconds: 5
            periodSeconds: 10
          resources:
            requests: { cpu: 100m, memory: 128Mi }
            limits:   { cpu: 200m, memory: 256Mi }
      volumes:
        - name: data
          persistentVolumeClaim: { claimName: snoopy-data }
```

`strategy: Recreate` and `replicas: 1` are load-bearing, not defaults to tune later — a rolling update would briefly run two copies of the bot on one token.

`pvc.yaml` is a 1 Gi `local-path` PVC named `snoopy-data`; each overlay sets its namespace so staging and prod get independent volumes. The probes target the bot's built-in health server (`web/health.py`): `/health` is pure liveness (process + event loop alive), `/ready` also checks the Discord connection, the database, and the scheduler.

## Secrets

Secrets stay out of git exactly as today, but move from `~/.env.snoopy` into a Kubernetes Secret per namespace, created once on the node from an env file (same format as `deploy/env.snoopy.example`):

```bash
kubectl -n chores-staging create secret generic chores-secrets --from-env-file=env.staging
kubectl -n chores         create secret generic chores-secrets --from-env-file=env.prod
```

`env.staging` carries the staging Discord token and staging Gemini key; `env.prod` carries the real ones. `GOOGLE_SA_JSON_B64` works unchanged — `entrypoint.sh` decodes it at container start in either environment. Delete the env files from disk after creating the secrets, or keep them only in `~/` with `chmod 600` as `~/.env.snoopy` is kept today.

## CI/CD

```
push to main
  → test             unit + SQLite integration tests (unchanged)
  → deploy-staging   SSH into node:
                       git pull
                       podman build -t localhost/snoopy-home:${GITHUB_SHA::7} .
                       podman save localhost/snoopy-home:${GITHUB_SHA::7} | sudo k3s ctr images import -
                       kubectl apply -k deploy/k8s/overlays/staging
                       kubectl -n chores-staging set image deploy/chores chores=localhost/snoopy-home:${GITHUB_SHA::7}
                       kubectl -n chores-staging rollout status deploy/chores --timeout=120s
                       podman image prune -f

manual "promote" (workflow_dispatch, GitHub Environment `production` with required reviewer)
  → deploy-prod      SSH into node:
                       kubectl apply -k deploy/k8s/overlays/prod
                       kubectl -n chores set image deploy/chores chores=localhost/snoopy-home:<sha>
                       kubectl -n chores rollout status deploy/chores --timeout=120s
```

Design decisions:

- **Build once, promote the same image.** Prod never rebuilds; it points at the exact sha-tagged image staging already ran. The promote workflow takes the sha as an input (defaulting to the latest staging sha).
- **Promotion is manual** via `workflow_dispatch` on a GitHub Environment named `production` with a required reviewer — GitHub then provides the approval gate and the audit trail. Verifying in staging means talking to the staging bot in the test server: mention it, set a reminder, confirm the reply and the scheduled fire.
- **`apply -k` always runs, and always before `set image`** — in that order in both jobs, every deploy. The base manifest carries a `:latest` placeholder tag, so an `apply -k` on its own would revert the running image to the placeholder (and `imagePullPolicy: Never` would then fail the next pod start). The overlays deliberately pin no image tag; the sha is applied imperatively as the last step. Corollary: never run `apply -k` by hand without following it with `set image` to the currently deployed sha (`kubectl -n chores get deploy/chores -o jsonpath='{.spec.template.spec.containers[0].image}'` shows it).
- **GitHub secrets are unchanged** — the same `OCI_VM_HOST` / `OCI_VM_USER` / `OCI_SSH_PRIVATE_KEY` reach the node; `kubectl` runs on the node itself (k3s kubeconfig), so the cluster API is never exposed to the internet.
- **Rollback** is `kubectl rollout undo deploy/chores -n chores` — or `set image` back to the previous sha, which is still in containerd.

## Cutover phases

- [ ] **Phase 0 — prerequisites.** k3s node up per the artifact (Traefik, cert-manager, local-path). Clone the repo onto the node (`git clone <repo-url> ~/snoopy_home` — the CI job's `git pull` assumes it exists). Give the deploy user working `kubectl`: a default k3s install writes `/etc/rancher/k3s/k3s.yaml` root-owned 0600, so bare `kubectl` fails for a non-root SSH user — either copy it to `~/.kube/config` (chowned to the user) or install k3s with `--write-kubeconfig-mode 644`; the CI job also needs passwordless sudo for `k3s ctr images import`. Create the staging Discord application in the Developer Portal, enable both privileged intents (Server Members + Message Content), invite it to a test server. Create a staging Gemini API key.
- [ ] **Phase 1 — manifests.** Add `deploy/k8s/` base + overlays to the repo. Create both namespaces (`kubectl create namespace chores-staging`, `kubectl create namespace chores` — `apply -k` does not create them) and both `chores-secrets`. Nothing deployed yet; `kubectl apply -k` dry-run passes.
- [ ] **Phase 2 — staging live.** Replace the `deploy` job in `.github/workflows/deploy.yml` with `deploy-staging`; add the `promote` workflow and the `production` GitHub Environment. Push to `main`, verify the staging bot responds in the test server and a reminder fires.
- [ ] **Phase 3 — prod cutover.** Order matters: the prod PVC's host directory does not exist until the PVC is provisioned, which local-path only does once a pod is scheduled against it — so the copy cannot come first. The sequence: (1) stop the Quadlet service on the old VM (`systemctl --user stop snoopy-home`) — two bots on one token must never overlap, so the old bot goes down before the new one comes up; (2) promote to prod (the promote workflow runs `apply -k` + `set image` with the staging-verified sha), letting the bot start once with an empty database — this provisions the PVC; (3) `kubectl -n chores scale deploy/chores --replicas=0`; (4) copy `snoopy_home.db` from the Quadlet volume into the PVC's host directory — find it with `kubectl get pv $(kubectl -n chores get pvc snoopy-data -o jsonpath='{.spec.volumeName}') -o jsonpath='{.spec.hostPath.path}'` (k3s local-path lives under `/var/lib/rancher/k3s/storage/`); (5) `kubectl -n chores scale deploy/chores --replicas=1`, verify with the real bot. Then retire the Quadlet permanently: on OL9, remove `~/.config/containers/systemd/snoopy-home.container` and run `systemctl --user daemon-reload` — Quadlet units are generated from the `.container` file and auto-enabled, so `systemctl --user disable` does not stick; on OL8, `systemctl --user disable --now snoopy-home`.
- [ ] **Phase 4 — decommission.** Retire the old VM (or the old service, if the k3s node is the same VM re-imaged). Mark DEPLOY.md as historical.
- [ ] **Phase 5 (later) — Postgres.** Move storage from SQLite to the shared Postgres 17 per the artifact: create database `chores` + role `chores_rw` (and `chores_staging` + `chores_staging_rw` for staging), port `storage/` from `aiosqlite` to an async Postgres driver, keep the pool at 5–10 connections per the artifact's connection budget (all apps together sit at ~45 of Postgres's default 100 connections; the artifact's escape hatch if that climbs is PgBouncer in transaction mode, not bigger pools). One-time data move via a script over the SQLite file. This phase is independent of the staging/prod split and can happen any time after Phase 3.

## Everyday operations (post-cutover)

| Task | Command (on node) |
|---|---|
| Live bot logs (prod) | `kubectl -n chores logs deploy/chores -f` |
| Live bot logs (staging) | `kubectl -n chores-staging logs deploy/chores -f` |
| Status | `kubectl -n chores get pods` |
| Restart | `kubectl -n chores rollout restart deploy/chores` |
| Roll back | `kubectl -n chores rollout undo deploy/chores` |
| Stop staging (free CPU) | `kubectl -n chores-staging scale deploy/chores --replicas=0` |
| Shell into container | `kubectl -n chores exec -it deploy/chores -- sh` |
| Inspect SQLite | `kubectl -n chores exec -it deploy/chores -- sqlite3 /data/snoopy_home.db` (or open the local-path dir on the host) |
