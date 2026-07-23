# Deploy plan — staging & prod on k3s (OCI A1.Flex)

**Status: ACTIVE for prod (design); staging premise superseded.** Prod is being
cut over to **single-node k3s on a fresh OCI A1.Flex node** — the runnable,
step-by-step procedure is [prod-k3s-runbook.md](../docs/prod-k3s-runbook.md), and
the manifests it applies live in [`deploy/k8s/`](k8s/). This document remains the
**design rationale** (topology, resource budget, `Recreate`/`replicas: 1`,
build-on-node-no-registry, `apply -k` before `set image`). Two premises here are
outdated and should be read accordingly: **staging now runs on minikube on the
local Mac laptop** (not co-located on the k3s node — see
[dev-stage-minikube-runbook.md](../docs/dev-stage-minikube-runbook.md)), so the staging overlay and
`deploy-staging` CI job below are historical; the k3s node runs **prod only** for
now, with the shared `data` Postgres ready to host transigen/gelp later.

Original artifact this plan was based on:
<https://claude.ai/code/artifact/55c5f771-9585-4198-b5e8-2d9175ae8c6b>

## Target topology (what the artifact defines)

One OCI Ampere A1.Flex node (ARM64, 2 OCPU, 12 GB RAM, 200 GB boot volume) running single-node k3s:

- **Ingress**: bundled Traefik, host-based routing, TLS via cert-manager + Let's Encrypt.
- **Runtime**: containerd, running images built on the node with Podman — no registry.
- **Storage**: local-path provisioner on the boot volume.
- **App layout**: one namespace per app. This bot is the `snoopy` app — 1 replica, ~0.2 CPU, 256 Mi, outbound egress to `generativelanguage.googleapis.com` (Gemini).
- **Data plane**: one shared Postgres 17 StatefulSet in namespace `data` (1 CPU, 1–2 Gi), one database + one role per app (`snoopy_home` / `snoopy_rw`), each role confined to its own database.
- **Deploy flow**: per-app repo → build on node with Podman → load image into containerd → `kubectl apply -k` (Kustomize overlay). No registry at this scale; the artifact defers Flux/GitOps until the cluster grows. The artifact triggers builds via webhook; this plan keeps the repo's existing GitHub Actions → SSH trigger instead, which lands on the same build-on-node flow with no new secrets.

Two constraints from the artifact worth keeping in mind: **CPU is the binding resource** on this node (RAM is plentiful), and per-app Postgres connection pools stay small (5–10).

Three deltas between the artifact and this repo today:

1. **Database** — resolved: the bot now runs on PostgreSQL (asyncpg, `storage/`), matching the artifact. Each environment gets its own database + role on the shared Postgres 17 in namespace `data`: prod `snoopy_home`/`snoopy_rw`, staging `snoopy_home_staging`/`snoopy_rw_staging`, each role confined to its own database, pool ≤ 5 connections per instance. Versioned migrations (`storage/migrations/*.sql`) run automatically at pod startup. No PVC is needed — the pod is stateless.
2. **Ingress** — the artifact routes `snoopy.example.com`, but the bot's only HTTP surface is an internal health/metrics port (8080: `/health`, `/ready`, `/metrics`); Discord is an outbound WebSocket and Gemini is an outbound API call. The port feeds the liveness/readiness probes and (later) Prometheus scraping — no Ingress or Service is created until the bot grows a user-facing web UI.
3. **Environments** — the artifact describes a single production cluster and defines no staging. The staging environment below is this plan's addition, carved out of the `snoopy` CPU allocation (see the node-budget note under Environments).

## Environments

| | Staging | Prod |
|---|---|---|
| Namespace | `snoopy-staging` | `snoopy` |
| Discord bot | **separate Discord application** + token, invited only to a test server | the real bot |
| Gemini key | separate API key (keeps quota/billing visible per env) | production key |
| Database | `snoopy_home_staging` (role `snoopy_rw_staging`) on `postgres.data.svc` | `snoopy_home` (role `snoopy_rw`) on `postgres.data.svc` |
| Image tag | `localhost/snoopy-home:<git-sha>` , auto-deployed | the same `<git-sha>` tag, promoted — never rebuilt |
| Deploy trigger | every push to `main` (after tests) | manual promotion with approval |
| Resources | requests 50m / 128 Mi, limits 200m / 256 Mi | requests 100m / 128 Mi, limits 200m / 256 Mi |

Full separation is not optional for a Discord bot: two instances sharing one bot token answer every message twice, and two schedulers sharing one database fire every reminder twice. Staging must have its own token, its own test server, and its own database file.

Both environments fit the node budget: prod + staging together request 0.15 CPU (150m), under the ~0.2 the artifact allocates to `snoopy`, with limits summing to 0.4 CPU for burst — acceptable while the other apps are below their caps, and staging can be scaled to zero (`kubectl -n snoopy-staging scale deploy/snoopy --replicas=0`) whenever the node gets tight.

## Repo layout to add

```
deploy/k8s/
  base/
    kustomization.yaml
    deployment.yaml
  overlays/
    staging/
      kustomization.yaml   # namespace: snoopy-staging, staging resource patch
    prod/
      kustomization.yaml   # namespace: snoopy, prod resource patch
```

Core of `base/deployment.yaml`:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: snoopy
spec:
  replicas: 1
  strategy:
    type: Recreate        # never two bots at once: duplicate replies on one token
  selector:
    matchLabels: { app: snoopy }
  template:
    metadata:
      labels: { app: snoopy }
    spec:
      containers:
        - name: snoopy
          image: localhost/snoopy-home:latest   # tag overridden per overlay
          imagePullPolicy: Never                # image is imported into containerd, never pulled
          envFrom:
            - secretRef: { name: snoopy-secrets }   # includes DATABASE_URL
          env:
            - { name: PYTHONUNBUFFERED, value: "1" }
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
```

`strategy: Recreate` and `replicas: 1` are load-bearing, not defaults to tune later — a rolling update would briefly run two copies of the bot on one token.

The pod is stateless (all state lives in the shared Postgres), so there is no PVC. The probes target the bot's built-in health server (`web/health.py`): `/health` is pure liveness (process + event loop alive), `/ready` also checks the Discord connection, the database, and the scheduler. Migrations (`python -m storage.migrate` semantics) run automatically at startup before the bot connects.

## Secrets

Secrets move into a Kubernetes Secret per namespace, created once on the node from an env file (plain `KEY=VALUE` lines — see `docs/prod-k3s-runbook.md` Gate 3):

```bash
kubectl -n snoopy-staging create secret generic snoopy-secrets --from-env-file=env.staging
kubectl -n snoopy         create secret generic snoopy-secrets --from-env-file=env.prod
```

`env.staging` carries the staging Discord token, staging Gemini key, and `DATABASE_URL=postgresql://snoopy_rw_staging:<pw>@postgres.data.svc:5432/snoopy_home_staging`; `env.prod` carries the real token/key and `DATABASE_URL=postgresql://snoopy_rw:<pw>@postgres.data.svc:5432/snoopy_home`. `GOOGLE_SA_JSON_B64` works unchanged — `entrypoint.sh` decodes it at container start in either environment. `shred` the env files from disk after creating the secrets.

## CI/CD

```
push to main
  → test             unit + SQLite integration tests (unchanged)
  → deploy-staging   SSH into node:
                       git pull
                       podman build -t localhost/snoopy-home:${GITHUB_SHA::7} .
                       podman save localhost/snoopy-home:${GITHUB_SHA::7} | sudo k3s ctr images import -
                       kubectl apply -k deploy/k8s/overlays/staging
                       kubectl -n snoopy-staging set image deploy/snoopy snoopy=localhost/snoopy-home:${GITHUB_SHA::7}
                       kubectl -n snoopy-staging rollout status deploy/snoopy --timeout=120s
                       podman image prune -f

manual "promote" (workflow_dispatch, GitHub Environment `production` with required reviewer)
  → deploy-prod      SSH into node:
                       kubectl apply -k deploy/k8s/overlays/prod
                       kubectl -n snoopy set image deploy/snoopy snoopy=localhost/snoopy-home:<sha>
                       kubectl -n snoopy rollout status deploy/snoopy --timeout=120s
```

Design decisions:

- **Build once, promote the same image.** Prod never rebuilds; it points at the exact sha-tagged image staging already ran. The promote workflow takes the sha as an input (defaulting to the latest staging sha).
- **Promotion is manual** via `workflow_dispatch` on a GitHub Environment named `production` with a required reviewer — GitHub then provides the approval gate and the audit trail. Verifying in staging means talking to the staging bot in the test server: mention it, set a reminder, confirm the reply and the scheduled fire.
- **`apply -k` always runs, and always before `set image`** — in that order in both jobs, every deploy. The base manifest carries a `:latest` placeholder tag, so an `apply -k` on its own would revert the running image to the placeholder (and `imagePullPolicy: Never` would then fail the next pod start). The overlays deliberately pin no image tag; the sha is applied imperatively as the last step. Corollary: never run `apply -k` by hand without following it with `set image` to the currently deployed sha (`kubectl -n snoopy get deploy/snoopy -o jsonpath='{.spec.template.spec.containers[0].image}'` shows it).
- **GitHub secrets are unchanged** — the same `OCI_VM_HOST` / `OCI_VM_USER` / `OCI_SSH_PRIVATE_KEY` reach the node; `kubectl` runs on the node itself (k3s kubeconfig), so the cluster API is never exposed to the internet.
- **Rollback** is `kubectl rollout undo deploy/snoopy -n snoopy` — or `set image` back to the previous sha, which is still in containerd.

## Cutover phases

- [ ] **Phase 0 — prerequisites.** k3s node up per the artifact (Traefik, cert-manager, local-path), shared Postgres 17 StatefulSet up in namespace `data`. Create both databases and roles on it (`CREATE DATABASE snoopy_home; CREATE ROLE snoopy_rw LOGIN PASSWORD '...'; GRANT ALL ON DATABASE snoopy_home TO snoopy_rw;` and the same for `snoopy_home_staging`/`snoopy_rw_staging` — each role confined to its own database). Clone the repo onto the node (`git clone <repo-url> ~/snoopy_home` — the CI job's `git pull` assumes it exists). Give the deploy user working `kubectl`: a default k3s install writes `/etc/rancher/k3s/k3s.yaml` root-owned 0600, so bare `kubectl` fails for a non-root SSH user — either copy it to `~/.kube/config` (chowned to the user) or install k3s with `--write-kubeconfig-mode 644`; the CI job also needs passwordless sudo for `k3s ctr images import`. Create the staging Discord application in the Developer Portal, enable both privileged intents (Server Members + Message Content), invite it to a test server. Create a staging Gemini API key.
- [ ] **Phase 1 — manifests.** Add `deploy/k8s/` base + overlays to the repo. Create both namespaces (`kubectl create namespace snoopy-staging`, `kubectl create namespace snoopy` — `apply -k` does not create them) and both `snoopy-secrets`. Nothing deployed yet; `kubectl apply -k` dry-run passes.
- [ ] **Phase 2 — staging live.** Replace the `deploy` job in `.github/workflows/deploy.yml` with `deploy-staging`; add the `promote` workflow and the `production` GitHub Environment. Push to `main`, verify the staging bot responds in the test server and a reminder fires.
- [ ] **Phase 3 — prod cutover.** The sequence: (1) stop the Quadlet service on the old VM (`systemctl --user stop snoopy-home`) — two bots on one token must never overlap, so the old bot goes down before the new one comes up; (2) apply the schema to the prod database: `python -m storage.migrate` with `DATABASE_URL` pointing at `snoopy_home` (or just let step 4's pod do it at startup); (3) move the data: copy `snoopy_home.db` off the old VM and run `python scripts/migrate_sqlite_to_pg.py --sqlite snoopy_home.db --pg postgresql://snoopy_rw:<pw>@<node>:.../snoopy_home` — it prints per-table source/dest row counts and exits non-zero on any mismatch; keep the SQLite file as the rollback artifact; (4) promote to prod (the promote workflow runs `apply -k` + `set image` with the staging-verified sha) and verify with the real bot. Then retire the Quadlet permanently: on OL9, remove `~/.config/containers/systemd/snoopy-home.container` and run `systemctl --user daemon-reload` — Quadlet units are generated from the `.container` file and auto-enabled, so `systemctl --user disable` does not stick; on OL8, `systemctl --user disable --now snoopy-home`.
- [ ] **Phase 4 — decommission.** Retire the old VM. The old Podman/Quadlet `DEPLOY.md` has been removed (git history retains it).
- [x] **Phase 5 — Postgres.** Done ahead of schedule, in the codebase rather than at cutover: `storage/` now runs on asyncpg with `asyncpg.create_pool(min_size=1, max_size=5)` per the artifact's connection budget (all apps together sit at ~45 of Postgres's default 100 connections; the escape hatch if that climbs is PgBouncer in transaction mode, not bigger pools). Versioned migrations live in `storage/migrations/`; the one-time data move is `scripts/migrate_sqlite_to_pg.py` (Phase 3 step 3). See `docs/storage.md`.

## Everyday operations (post-cutover)

| Task | Command (on node) |
|---|---|
| Live bot logs (prod) | `kubectl -n snoopy logs deploy/snoopy -f` |
| Live bot logs (staging) | `kubectl -n snoopy-staging logs deploy/snoopy -f` |
| Status | `kubectl -n snoopy get pods` |
| Restart | `kubectl -n snoopy rollout restart deploy/snoopy` |
| Roll back | `kubectl -n snoopy rollout undo deploy/snoopy` |
| Stop staging (free CPU) | `kubectl -n snoopy-staging scale deploy/snoopy --replicas=0` |
| Shell into container | `kubectl -n snoopy exec -it deploy/snoopy -- sh` |
| Inspect the database | `kubectl -n data exec -it statefulset/postgres -- psql -U snoopy_rw snoopy_home` |
