# Prod provisioning + running the app in dev / stage / prod

Reference notes written after the Postgres/eval/function-calling/multi-tenancy
upgrade (`docs/UPGRADE-PLAN.md`). The code is DB-agnostic between
orchestration paths — moving prod to a different orchestrator later is just
a new `DATABASE_URL` and deploy pipeline, not a code change.

**Environment split:** prod runs on the OCI VM; staging runs on **minikube
on the local Mac laptop**, entirely decoupled from prod's infrastructure.
This means k3s is no longer a prerequisite for having a staging
environment — minikube gives real Kubernetes namespacing for free, locally,
without touching the shared prod box. `deploy/PLAN-DEPLOY-K3S.md`'s original plan
assumed staging and prod co-located on one k3s node; that assumption is
superseded by this split. k3s for prod itself remains a possible later move,
but it's now an independent decision, not something staging depends on.

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

## 1. Provisioning Postgres on prod (OCI VM)

> **⚠️ Superseded.** This section described provisioning Postgres as a
> Podman/Quadlet container alongside the bot on the existing VM. Prod has since
> moved to **single-node k3s on a fresh OCI A1.Flex node**, with Postgres as the
> shared `data`-namespace Deployment (`deploy/k8s/postgres.yaml`) instead of a
> Quadlet. Follow **[prod-k3s-runbook.md](prod-k3s-runbook.md)** for the current
> cutover. The Podman/Quadlet notes below are retained only for the old box until
> it is decommissioned. §2 (staging on minikube) and §3 (dev) remain current.

The only genuinely new infrastructure requirement from the upgrade is
**Postgres** on the VM that's already running the bot. Single-VM +
Podman/Quadlet stays the right choice here — it's the existing live setup,
and the original reason to consider k3s for prod (clean staging isolation)
no longer applies since staging lives on minikube instead.

### Steps (on the OCI VM, as `opc`)

```bash
# 1. Shared network so the bot container can reach Postgres by name
podman network create snoopy-net

# 2. Postgres 17 as a Quadlet unit with a persistent volume
mkdir -p ~/.config/containers/systemd
cat > ~/.config/containers/systemd/snoopy-pg.container <<'EOF'
[Unit]
Description=Postgres 17 for Snoopy Home
[Container]
Image=docker.io/library/postgres:17
ContainerName=snoopy-pg
Network=snoopy-net
Volume=snoopy-pgdata:/var/lib/postgresql/data
EnvironmentFile=%h/.env.postgres
[Service]
Restart=always
[Install]
WantedBy=default.target
EOF

# ~/.env.postgres (chmod 600) — superuser bootstrap only
printf 'POSTGRES_PASSWORD=%s\n' 'STRONG_SUPERUSER_PW' > ~/.env.postgres
chmod 600 ~/.env.postgres

systemctl --user daemon-reload && systemctl --user start snoopy-pg
```

Create the app database **owned by the app role** — this sidesteps the
Postgres 15+ gotcha where a plain `GRANT ALL ON DATABASE` still can't
`CREATE TABLE` in the `public` schema; ownership avoids it and migrations
run cleanly:

```bash
podman exec -it snoopy-pg psql -U postgres -c \
  "CREATE ROLE snoopy_rw LOGIN PASSWORD 'STRONG_APP_PW';" -c \
  "CREATE DATABASE snoopy_home OWNER snoopy_rw;"
```

Then in `~/.env.snoopy`:

```
DATABASE_URL=postgresql://snoopy_rw:STRONG_APP_PW@snoopy-pg:5432/snoopy_home
```

Both containers must share the network — add `Network=snoopy-net` to the
bot's own `.container` unit too. No PVC/volume needed for the bot anymore —
it's stateless (all state lives in Postgres).

### Data migration

Stop the old bot first — never run two bots on one Discord token
simultaneously. Then:

```bash
python scripts/migrate_sqlite_to_pg.py \
  --sqlite snoopy_home.db \
  --pg "postgresql://snoopy_rw:STRONG_APP_PW@localhost:5432/snoopy_home"
```

It verifies row counts per table and exits non-zero on any mismatch. Schema
is created automatically — `main.py` runs migrations at startup, or run
`python -m storage.migrate` manually first.

### Before the first prod deploy

Two things must be fixed or the next push crashes the bot on startup:

1. **CI `deploy` job** needs `DATABASE_URL` present in `~/.env.snoopy` on the
   VM, and the bot's Quadlet unit needs the shared network wired in
   (`Network=snoopy-net`).
2. **Env example files** (`.env.example`, `deploy/env.snoopy.example`) still
   document `DB_PATH`/SQLite only — they should be updated to show
   `DATABASE_URL` so future setup doesn't regress to SQLite.

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

### Availability caveat

Staging is only up when the laptop is on and `minikube start` has been run
— unlike prod (always-on VM), this is intentionally an on-demand
verification environment, not a 24/7 service. Set a reminder to spin it up
before testing, not to expect it always answering in the test server.

Verify staging by talking to the staging bot in a test server: set a
reminder, confirm it fires; smoke-test a tools-mode query (e.g. "what
chores do we have?") to confirm function calling works end-to-end.

### `deploy/k8s/` manifests — still to author

Base Deployment + staging/prod overlays don't exist in the repo yet (see
`deploy/PLAN-DEPLOY-K3S.md`'s "Repo layout to add"). Since minikube is a real
Kubernetes cluster, the same base manifest works for both a future
prod-on-k3s and staging-on-minikube — only the target context differs
(`kubectl config use-context minikube` vs whatever the OCI k3s node uses).
Writing these is the next concrete step before the `kubectl apply` commands
above will actually work.

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

### Prod (OCI VM)

```bash
git push origin main
# CI: tests → SSH to VM → git pull → podman build → systemctl --user restart snoopy-home
```

Observe: `sudo journalctl CONTAINER_NAME=systemd-snoopy-home -f` and
`curl localhost:8080/ready`.

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
- `deploy/k8s/postgres.yaml` (Postgres, shared by dev + staging) exists;
  the bot's own manifests (base Deployment + staging/prod overlays) don't —
  needed before the `kubectl apply -k` commands in §2's deploy flow will
  run.
