# Prod provisioning + running the app in dev / stage / prod

Reference notes written after the Postgres/eval/function-calling/multi-tenancy
upgrade (`docs/UPGRADE-PLAN.md`). The code is DB-agnostic between
orchestration paths — moving prod to a different orchestrator later is just
a new `DATABASE_URL` and deploy pipeline, not a code change.

**Environment split:** prod runs on **single-node k3s** on a fresh OCI
A1.Flex node; staging runs on **minikube on the local Mac laptop**, entirely
decoupled from prod's infrastructure. The two are separate clusters — minikube
gives staging real Kubernetes namespacing for free, locally, without touching
the prod box. `deploy/PLAN-DEPLOY-K3S.md`'s original plan co-located staging and
prod on one k3s node; that assumption is superseded by this split (staging on
minikube, prod on its own k3s node).

**Dev and staging share one Postgres instance, on purpose.** Both run
locally on the same Mac, so as of 2026-07-19 there is a single `postgres:17`
Deployment inside minikube (`deploy/k8s/postgres.yaml`, namespace
`snoopy-staging`) — no separate podman-hosted Postgres for dev anymore.
Local `python main.py` reaches it over a `kubectl port-forward` tunnel to
`localhost:5432`; the staging bot pod reaches it in-cluster via the
`postgres.snoopy-staging.svc` Service DNS. Two live Postgres *server
processes* can never share one data directory (Postgres locks it with
`postmaster.pid`; forcing past the lock corrupts the data) — sharing data
means exactly one server, reached from both sides. This is orthogonal to
the prod-isolation rule below: prod still never shares data with dev or
staging. The one condition this setup depends on: **run the dev bot
process and the staging bot pod one at a time, never both live
simultaneously** — two schedulers against the same rows will race on
reminders and chore/todo state, the same hazard `PLAN-DEPLOY-K3S.md` calls out
for any shared database.

## 1. Prod — single-node k3s on the OCI node

Prod runs on **single-node k3s** on a fresh OCI A1.Flex node: the bot in
namespace `snoopy`, backed by the shared Postgres 17 in namespace `data`
(`deploy/k8s/postgres.yaml`), rolled out by the `v*`-tag-gated CI job in
`.github/workflows/deploy.yml`. The full provisioning + cutover procedure —
install k3s, provision Postgres + the `snoopy_home`/`snoopy_rw` database, stop
the old bot, migrate SQLite→Postgres, deploy, verify — is
**[prod-k3s-runbook.md](prod-k3s-runbook.md)**; the design rationale is
`deploy/PLAN-DEPLOY-K3S.md`.

The earlier single-VM Podman/Quadlet path (bot + a `snoopy-pg` Quadlet Postgres,
`~/.env.snoopy`, `systemctl --user restart`) has been removed now that k3s
supersedes it; `git log` retains it if you need to operate the old box before it
is decommissioned.

## 2. Staging — minikube on the local Mac

Staging must have its **own Discord application/token** — never share
prod's. Two bots on one token answer every message twice. Running staging
on minikube instead of the OCI VM keeps it fully isolated from prod's
resources (the VM is only 1 OCPU/6 GB) and gives real k8s namespacing
without needing k3s on the shared box at all. Staging's *database*, unlike
its Discord token, is intentionally shared — with dev, not with prod; see
the note above.

### Setup

```bash
./deploy/setup-minikube.sh   # or POSTGRES_PASSWORD=<pw> ./deploy/setup-minikube.sh
```

This brings up minikube (podman driver, pinned to `containerd` — the default
`cri-o` crashes on this podman/macOS combo), enables real etcd
encryption-at-rest for k8s Secrets (see script header comment for why this
needs more than `minikube start`), and applies the shared Postgres —
namespace, PVC, Deployment, Service all come from one manifest
(`deploy/k8s/postgres.yaml`). This is the same instance dev's local `python
main.py` connects to (see below), not a staging-only copy. The Secret is
deliberately not in that manifest (a committed Secret is a plaintext
password in git); the script creates it from a shell variable, defaulting to
`dev`. **Recreating the cluster from scratch (`minikube delete`) means
re-running this script**, not a plain `minikube start` — otherwise
encryption-at-rest silently reverts to off.

```bash
# Discord bot needs no inbound networking — it's an outbound WebSocket to
# Discord's gateway and outbound HTTPS to Gemini, so it works fine behind
# home NAT with no port forwarding.
kubectl -n snoopy-staging create secret generic snoopy-secrets \
  --from-env-file=env.staging   # staging Discord token, staging Gemini key, DATABASE_URL
```

### Deploy flow — manual (decision made explicitly: no self-hosted CI runner)

GitHub Actions can't SSH into a laptop behind NAT/dynamic IP the way it
reaches the OCI VM's public IP, so staging deploys are **manual, run from
the laptop**, not automated in CI:

```bash
git pull
minikube image build -t localhost/snoopy-home:staging .   # or: eval $(minikube docker-env) && podman build ...
kubectl apply -k deploy/k8s/overlays/staging               # to author
kubectl -n snoopy-staging set image deploy/snoopy snoopy=localhost/snoopy-home:staging
kubectl -n snoopy-staging rollout status deploy/snoopy
```

This was a deliberate tradeoff: a self-hosted GitHub Actions runner on the
laptop would restore "push to main auto-deploys to staging," but means
giving a persistent GitHub-controlled agent execution access to a personal
machine. Manual deploy avoids that at the cost of remembering to run it.

### Checking staging Postgres status

```bash
minikube status                                    # is the cluster itself up?
kubectl -n snoopy-staging get pods                  # is the postgres Pod Running?
kubectl -n snoopy-staging describe pod -l app=postgres   # recent events / restart reasons if it's not
kubectl -n snoopy-staging exec -it deploy/postgres -- pg_isready   # is it accepting connections?
```

`podman ps` won't show the Postgres container — minikube's podman driver
only runs the cluster *node* itself (image `kicbase`, container name
`minikube`) as a podman container; everything inside the node (the
Postgres Pod included) is managed by containerd, one layer down, and only
`kubectl`/`minikube` can see it:

```bash
minikube ssh -- sudo crictl ps   # containerd's view from inside the node, if you need to confirm that layer directly
```

### Getting a shell / psql prompt in staging Postgres

```bash
kubectl -n snoopy-staging exec -it deploy/postgres -- bash            # shell in the container
kubectl -n snoopy-staging exec -it deploy/postgres -- psql -U postgres   # straight to a psql prompt
```

`deploy/postgres` resolves to whichever Pod the Deployment currently owns,
so it keeps working across Pod restarts (unlike naming the Pod directly,
which changes on every restart).

### Checking what driver/backend minikube uses

```bash
minikube profile list                                            # driver/runtime/status per profile
minikube config get driver                                       # just the configured driver
cat ~/.minikube/profiles/minikube/config.json | grep -i driver    # same info, straight from the profile config
```

This project's minikube runs the `podman` driver with the `containerd`
runtime — that's why `podman ps` only shows the node container and not the
Pods inside it (see above).

### Availability caveat

Staging is only up when the laptop is on and `minikube start` has been run
— unlike prod (always-on VM), this is intentionally an on-demand
verification environment, not a 24/7 service. Set a reminder to spin it up
before testing, not to expect it always answering in the test server.

Verify staging by talking to the staging bot in a test server: set a
reminder, confirm it fires; smoke-test a tools-mode query (e.g. "what
chores do we have?") to confirm function calling works end-to-end.

### `deploy/k8s/` manifests

The bot's base Deployment + prod overlay now exist (`deploy/k8s/base`,
`deploy/k8s/overlays/prod`). Since minikube is a real Kubernetes cluster, the
same base manifest serves both prod-on-k3s and staging-on-minikube — only the
target context differs (`kubectl config use-context minikube` vs the OCI k3s
node). A dedicated staging overlay isn't in the repo yet; the `kubectl apply -k
overlays/staging` command above is aspirational until one is added (staging can
apply the base directly in the meantime).

## 3. Running the app: dev / stage / prod, summarized

### Dev (local, macOS) — now depends on minikube, by design

Dev's Postgres is the same minikube-hosted instance staging uses (§2) — not
a standalone podman container anymore. Reach it via a port-forward tunnel:

```bash
minikube start --driver=podman --container-runtime=containerd  # if not already running; resuming an existing cluster, safe
# if the cluster doesn't exist yet (fresh or after `minikube delete`), use
# ./deploy/setup-minikube.sh instead — plain `minikube start` won't set up
# etcd encryption-at-rest on a from-scratch cluster (see §2)
kubectl apply -f deploy/k8s/postgres.yaml    # idempotent; no-op if already applied
kubectl -n snoopy-staging port-forward svc/postgres 5432:5432 &   # keep running while you work

# .env needs: DISCORD_TOKEN, GEMINI_API_KEY, DATABASE_URL (no code default —
# see .env.example; postgresql://postgres:dev@localhost:5432/snoopy_home still
# works here, only what's listening on localhost:5432 changed, from a podman
# container to this port-forward tunnel)

python main.py            # runs migrations → /health on :8080 → starts bot

# tests (one-time): createdb snoopy_test
pytest tests/
python -m evals.runner --judge
```

The port-forward has to be a separate long-lived process — it doesn't
survive closing the terminal/session it was started in. If `python
main.py` fails at the migration step with an `asyncpg`
`OSError`/`Connect call failed` on `127.0.0.1:5432`, this tunnel isn't
running; check with `lsof -nP -iTCP:5432 -sTCP:LISTEN` (empty output means
re-run the `port-forward` command above) — see "Checking staging Postgres
status" in §2 for the fuller diagnostic sequence.

Remember the rule from the note above: don't run this alongside the
staging bot pod at the same time — they'd both be scheduling reminders and
writing chore/todo state against the same rows.

### Staging (minikube, local Mac)

See §2 above — `minikube start` → build/load image → `kubectl apply -k
overlays/staging` → verify in a test Discord server, all run manually from
the laptop.

### Prod (OCI k3s node)

```bash
git tag v1.2.0 && git push origin v1.2.0
# CI: tests → SSH to node → podman build → k3s ctr images import → apply -k → set image → rollout status
```

Observe: `kubectl -n snoopy logs deploy/snoopy -f` and
`kubectl -n snoopy rollout status deploy/snoopy`. Full procedure:
[prod-k3s-runbook.md](prod-k3s-runbook.md).

### Health/metrics — identical everywhere

| Endpoint | Purpose |
|---|---|
| `/health` | liveness (process + event loop alive) |
| `/ready` | readiness (Discord connected + DB reachable + scheduler running) |
| `/metrics` | Prometheus exposition |

All on port 8080 (`web/health.py`).

## Known gaps

- The tools-mode Discord smoke test and two-guild isolation check need a
  real second server invite and a live bot connection — exercise these
  once on staging (minikube) before promoting a change to prod.
- The bot's base Deployment + prod overlay now exist (`deploy/k8s/base`,
  `deploy/k8s/overlays/prod`); a dedicated staging overlay does not — staging
  applies the base manifest directly for now.
