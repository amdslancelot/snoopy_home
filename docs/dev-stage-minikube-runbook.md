# Dev & staging runbook — minikube on the local Mac

How to run the bot in **dev** (local `python main.py`) and **staging** (a bot
pod), both against the minikube cluster on the Mac. **Prod is a separate
cluster** — single-node k3s on an OCI node — with its own procedure in
[prod-k3s-runbook.md](prod-k3s-runbook.md); it is intentionally decoupled from
everything here (prod never shares data with dev or staging).

**Dev and staging share one Postgres instance, on purpose.** Both run
locally on the same Mac, so as of 2026-07-19 there is a single `postgres:17`
Deployment inside minikube (`deploy/k8s/postgres.yaml`, in the neutral
`data` namespace) — no separate podman-hosted Postgres for dev anymore.
Local `python main.py` reaches it over a `kubectl port-forward` tunnel to
`localhost:5432`; the staging bot pod reaches it in-cluster via the
`postgres.data.svc` Service DNS. Two live Postgres *server
processes* can never share one data directory (Postgres locks it with
`postmaster.pid`; forcing past the lock corrupts the data) — sharing data
means exactly one server, reached from both sides. This is orthogonal to
the prod-isolation rule: prod still never shares data with dev or staging. The one condition this setup depends on: **run the dev bot
process and the staging bot pod one at a time, never both live
simultaneously** — two schedulers against the same rows will race on
reminders and chore/todo state, the same hazard `PLAN-DEPLOY-K3S.md` calls out
for any shared database.

## 0. What's what — identities and resources per environment

Verified 2026-11-21. **There is no dev bot:** dev (`python main.py` on the
Mac) and staging (the minikube pod) are one environment run two ways. They
share the *staging* Discord bot, the *stage* calendar and the shared Postgres.
Only prod has its own of each.

| | Dev (laptop process) | Staging (minikube pod) | Prod (OCI k3s pod) |
|---|---|---|---|
| Discord bot | **staging bot**, user ID `1528663014994088037` (token in `.env`) | staging bot (same token, via `env.staging` → secret) | prod bot, user ID `1516910404977164461` |
| Discord server | **Snoopy Home Staging** (the bot's only server) | Snoopy Home Staging | Snoopy Home |
| Google Calendar | **Snoopy Home Stage** (`HOUSEHOLD_CALENDAR_ID` in `.env`) | Snoopy Home Stage | Snoopy Home |
| Google identity | service account `ais-gemini-key-…@…iam.gserviceaccount.com` | same | **same** (see Known gaps) |
| Postgres | minikube `data` ns, db `snoopy_home`, via port-forward | same db, via `postgres.data.svc` | separate server on the OCI node |
| Runs when | you run `python main.py` | never deployed yet (no staging overlay) | always |

Test dev/staging changes by tagging `@snoopy_home` **in the Snoopy Home
Staging server**. Events it creates land on the **Snoopy Home Stage**
calendar; tick that calendar in Google Calendar to see them. Profile
features (e.g. attendee emails) need members of the staging server.

To check which bot a token belongs to without printing the token, decode its
first segment (the bot's user ID):

```bash
grep '^DISCORD_TOKEN=' .env | cut -d= -f2 | cut -d. -f1 | base64 -d; echo   # prints the bot user ID only
```

**Never both live.** Because dev and staging share the bot token *and* the
database, running `python main.py` while a staging pod is up gives double
replies (Discord delivers every message to every connection on a token),
double writes, and every reminder firing twice (each process loads all active
reminders into its own scheduler). Before starting dev:

```bash
kubectl -n snoopy-staging get deploy snoopy 2>/dev/null   # no output = no staging deployment ✅ · 0/0 = scaled down ✅ · 1/1 = scale it to 0 first ❌
```

**Databases on the shared Postgres** (`psql -U postgres -l`): `snoopy_home`
(dev + staging data), `snoopy_test` (pytest scratch, wiped on every run — see
§2), plus `gelp` and `transigen` from other projects and the Postgres
defaults. Prod's server has no `snoopy_test`, by design: CI runs the
integration tests against its own throwaway `postgres:17` service container.

**Legacy:** the pre-2026-07-19 dev database, podman container `snoopy-pg`
(`postgres:17`), still exists in the *Exited* state with an anonymous 48.6 MB
volume. Nothing uses it. To remove it, back it up first, and note that
`podman rm` without `-v` leaves the volume behind:

```bash
podman start snoopy-pg                                       # stop the minikube port-forward first: both want :5432
podman exec snoopy-pg pg_dumpall -U postgres > ~/snoopy-pg-final.sql   # one-time backup
podman stop snoopy-pg && podman rm -v snoopy-pg              # -v also removes its anonymous data volume
```

## 1. Staging — minikube on the local Mac

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

`env.staging` does not exist yet. Build it from `.env`, with two differences
for running in a pod (keep `env.staging` gitignored, it holds secrets):

- `DATABASE_URL` host `localhost:5432` → `postgres.data.svc.cluster.local:5432`
  (in-cluster Service; the pod doesn't use the port-forward).
- Replace the file path `GOOGLE_SERVICE_ACCOUNT_JSON=…` with
  `GOOGLE_SA_JSON_B64=$(base64 -i <key.json> | tr -d '\n')`; `entrypoint.sh`
  decodes it to `/app/service_account.json` at startup (same as prod).

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
kubectl -n data get pods                  # is the postgres Pod Running?
kubectl -n data describe pod -l app=postgres   # recent events / restart reasons if it's not
kubectl -n data exec -it deploy/postgres -- pg_isready   # is it accepting connections?
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
kubectl -n data exec -it deploy/postgres -- bash            # shell in the container
kubectl -n data exec -it deploy/postgres -- psql -U postgres   # straight to a psql prompt
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

Verify staging by talking to the staging bot in the **Snoopy Home Staging**
server (§0): set a
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

## 2. Running the app: dev / stage, summarized

### Dev (local, macOS) — now depends on minikube, by design

Dev's Postgres is the same minikube-hosted instance staging uses (§1) — not
a standalone podman container anymore. Reach it via a port-forward tunnel:

```bash
podman machine start # boot up linux virtual machine required to run containers on macOS (default set of off on personal laptop)
minikube start --driver=podman --container-runtime=containerd  # if not already running; resuming an existing cluster, safe
# if the cluster doesn't exist yet (fresh or after `minikube delete`), use
# ./deploy/setup-minikube.sh instead — plain `minikube start` won't set up
# etcd encryption-at-rest on a from-scratch cluster (see §1)
kubectl config use-context minikube         # make sure kubectl is NOT pointed at prod
kubectl -n snoopy-staging get deploy snoopy 2>/dev/null   # must print nothing or 0/0 (§0: never both live)
kubectl apply -f deploy/k8s/postgres.yaml    # idempotent; no-op if already applied
kubectl -n data port-forward svc/postgres 5432:5432 > /dev/null 2>&1 &   # keep running while you work

# .env needs: DISCORD_TOKEN (the STAGING bot's — §0), GEMINI_API_KEY,
# DATABASE_URL=postgresql://snoopy_rw:dev@localhost:5432/snoopy_home (no code
# default — see .env.example), and for calendar features
# GOOGLE_SERVICE_ACCOUNT_JSON + HOUSEHOLD_CALENDAR_ID (the Stage calendar)

python main.py 2>&1 | tee /tmp/snoopy-dev.log   # runs migrations → /health on :8080 → starts bot as the staging bot
```

Python loads the code once at startup: **after any code change, Ctrl-C and
start `python main.py` again**, or you are testing the old code. (On
2026-11-21 a live test ran against code from before the fix being tested; the
process start time `ps -o lstart= -p <pid>` vs file mtimes showed it.)

Tests and evals:

```bash
# one-time: the throwaway DB the integration tests wipe and rebuild each run
kubectl -n data exec deploy/postgres -- psql -U postgres -c "CREATE DATABASE snoopy_test OWNER snoopy_rw;"

export TEST_DATABASE_URL=postgresql://snoopy_rw:dev@localhost:5432/snoopy_test   # shell env, NOT .env (see below)
pytest tests/ -rs              # -rs prints why anything was skipped
python -m evals.runner --judge
```

- ⚠️ **`TEST_DATABASE_URL` must never point at `snoopy_home`.** The test
  fixture (`tests/conftest.py`) runs `DROP SCHEMA public CASCADE` before every
  test.
- **Put it in your shell, not `.env`.** `config.py` (pydantic-settings)
  rejects unknown keys in `.env` with `Extra inputs are not permitted`, which
  breaks every import of `config`, including the bot's. The fixture reads it
  from `os.environ` anyway.
- **All integration tests skipped?** Run `pytest -rs`: `Connect call failed
  ('127.0.0.1', 5432)` means the port-forward isn't running. Integration
  tests skip silently without it, so a green run can be unit tests only.
- `tests/integration/test_google_calendar.py` creates and deletes events on
  the **real** calendar in `HOUSEHOLD_CALENDAR_ID`; run it deliberately.

The port-forward has to be a separate long-lived process — it doesn't
survive closing the terminal/session it was started in. If `python
main.py` fails at the migration step with an `asyncpg`
`OSError`/`Connect call failed` on `127.0.0.1:5432`, this tunnel isn't
running; check with `lsof -nP -iTCP:5432 -sTCP:LISTEN` (empty output means
re-run the `port-forward` command above) — see "Checking staging Postgres
status" in §1 for the fuller diagnostic sequence.

Remember the rule from the note above: don't run this alongside the
staging bot pod at the same time — they'd both be scheduling reminders and
writing chore/todo state against the same rows.

### Staging (minikube, local Mac)

See §1 above — `minikube start` → build/load image → `kubectl apply -k
overlays/staging` → verify in a test Discord server, all run manually from
the laptop.

(Prod deploys are a separate cluster and procedure —
[prod-k3s-runbook.md](prod-k3s-runbook.md).)

### Health/metrics — dev & staging (same endpoints as prod)

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
  applies the base manifest directly for now. Consequently the staging pod
  has never been deployed; "staging" testing so far has meant dev
  (`python main.py`) logged in as the staging bot.
- **One Google service account serves dev, staging and prod.** It can edit
  both the Snoopy Home Stage and the Snoopy Home (prod) calendars, so only
  `HOUSEHOLD_CALENDAR_ID` in `.env` keeps dev testing off the real household
  calendar. Fix: a staging-only service account shared with the Stage
  calendar alone. The prod key also still needs rotating (its private key was
  pasted in plaintext during the 2026-07-22 prod deploy).
- **Calendar invites can't be sent.** A service account on a consumer
  (@gmail.com) calendar gets 403 "Service accounts cannot invite attendees
  without Domain-Wide Delegation of Authority" (that only exists on Google
  Workspace). Since v1.1.0 the bot re-creates the event without attendees and
  notes them in the description. Real invites would need the bot to act as a
  user via OAuth.
- `.env.example` says the dev password `dev` must never be reused in
  staging, but staging shares dev's database and therefore its password.
