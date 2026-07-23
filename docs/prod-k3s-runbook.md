# Prod cutover runbook — snoopy on k3s (fresh OCI A1.Flex node)

The prior live prod was a single-VM **Podman/Quadlet + SQLite** bot. This runbook
stands up prod on **single-node k3s** on a **fresh** OCI Ampere A1.Flex instance
(2 OCPU / 12 GB / 200 GB, Oracle Linux 9), with the bot on PostgreSQL and a
shared Postgres in the `data` namespace ready for transigen/gelp later. It
replaces the earlier Podman/Quadlet prod path, which has been removed from the
docs (`git log` retains it).

Design and manifests: `deploy/PLAN-DEPLOY-K3S.md`, `deploy/k8s/`.

---

## Gate 0 — provision the instance

OCI Console → Compute → Instances → Create:
- Image **Oracle Linux 9**, shape `VM.Standard.A1.Flex`, **2 OCPU / 12 GB**, 200 GB boot volume.
- Public IP; upload the same deploy SSH public key you use today.
- **Security List / NSG:** the bot needs no inbound ports (Discord + Gemini are
  outbound). Leave inbound at just SSH (22). k3s is single-node, so its API
  (6443) never needs to be exposed.

```bash
ssh opc@<NEW_VM_IP>
sudo dnf install -y git podman
```

## Gate 1 — install k3s

k3s on OL9: firewalld interferes with the CNI on a single node — the standard
fix is to disable it and rely on the OCI Security List for the firewall (which
you already do today).

```bash
sudo systemctl disable --now firewalld

# --disable traefik,servicelb: the bot has no inbound HTTP (health/metrics on
#   8080 is scraped in-cluster, not ingressed), so we drop both to save CPU/RAM.
# --write-kubeconfig-mode 644: makes the kubeconfig readable by opc so bare
#   `kubectl` works over a non-interactive SSH deploy.
curl -sfL https://get.k3s.io | \
  INSTALL_K3S_EXEC="--disable traefik --disable servicelb --write-kubeconfig-mode 644" sh -

# The installer symlinks `kubectl`, `ctr`, `crictl` → k3s, and the k3s-wrapped
# kubectl defaults to /etc/rancher/k3s/k3s.yaml (now 644-readable). Belt-and-
# suspenders for standalone kubectl later:
mkdir -p ~/.kube && sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config && sudo chown opc:opc ~/.kube/config

# CI runs `sudo k3s ctr images import` non-interactively — allow it without a password:
echo 'opc ALL=(ALL) NOPASSWD: /usr/local/bin/k3s' | sudo tee /etc/sudoers.d/k3s-ctr

# Clone the repo where CI expects it:
git clone https://github.com/<YOUR_USERNAME>/<YOUR_REPO>.git ~/snoopy_home
# (private repo: embed a read-scope PAT — https://<user>:<PAT>@github.com/...)

kubectl get nodes   # Ready within ~30s
```

## Gate 2 — shared Postgres in the `data` namespace

```bash
cd ~/snoopy_home

# Superuser bootstrap password — provisioning only; the app never uses it.
kubectl create namespace data
POSTGRES_PASSWORD='<STRONG_SUPERUSER_PW>' && \
  kubectl create secret generic postgres-secret -n data \
    --from-literal=POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
    --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -f deploy/k8s/postgres.yaml            # PVC + Deployment + Service in `data`
kubectl -n data rollout status deploy/postgres
```

Create the app database **owned by** the app role (ownership sidesteps the
Postgres 15+ `public`-schema grant gotcha so migrations run cleanly), and confine
the role to its own database:

```bash
kubectl -n data exec -it deploy/postgres -- psql -U postgres \
  -c "CREATE ROLE snoopy_rw LOGIN PASSWORD '<STRONG_APP_PW>';" \
  -c "CREATE DATABASE snoopy_home OWNER snoopy_rw;" \
  -c "REVOKE CONNECT ON DATABASE snoopy_home FROM PUBLIC;"
```

> Note: `deploy/k8s/postgres.yaml` sets `POSTGRES_DB: snoopy_home`, so the
> Postgres image may already create an empty `snoopy_home` owned by `postgres` on
> first boot. If the `CREATE DATABASE` above errors "already exists", instead run
> `ALTER DATABASE snoopy_home OWNER TO snoopy_rw;` so `snoopy_rw` owns it.

## Gate 3 — the app secret

```bash
cat > env.prod <<'EOF'
DISCORD_TOKEN=<prod bot token>
GEMINI_API_KEY=<prod Gemini key>
DATABASE_URL=postgresql://snoopy_rw:<STRONG_APP_PW>@postgres.data.svc:5432/snoopy_home
GOOGLE_SA_JSON_B64=<base64 of the service-account JSON, or omit>
EOF

kubectl create namespace snoopy
kubectl -n snoopy create secret generic snoopy-secrets --from-env-file=env.prod
shred -u env.prod          # don't leave the token on disk
```

## Gate 4 — 🛑 stop the OLD bot, grab the SQLite file

On the **OLD** VM (not the new node):

```bash
ssh opc@<OLD_VM_IP> 'systemctl --user stop snoopy-home'
# copy the live DB straight to the new node's repo dir for the migration:
scp opc@<OLD_VM_IP>:/home/opc/.local/share/containers/storage/volumes/snoopy-data/_data/snoopy_home.db \
    opc@<NEW_VM_IP>:~/snoopy_home/snoopy_home.db
```

From here the bot is **down** — keep Gates 5–6 tight. Keep this `.db` as the
rollback artifact; do not delete it.

## Gate 5 — migrate schema + data (before any pod starts)

The Postgres Service isn't exposed outside the cluster, so tunnel to it on the
node, then run the migrations inside the built image (which carries asyncpg
etc.) with the repo mounted so the code matches. Two OL9-specific notes baked
into the commands below:

- **`:z` on the bind mount** — OL9 runs SELinux enforcing, which blocks an
  unlabeled bind mount; the container would see an empty `/app` and fail with
  `No module named 'storage'`. `:z` relabels the dir so the container can read
  it. (The SQLite file must come through this mount — `.dockerignore` excludes
  `*.db`, so it is *not* in the image.)
- **stub `DISCORD_TOKEN`/`GEMINI_API_KEY` on step 1** — `storage.migrate` imports
  `config`, whose `Settings()` requires both fields at construction even though
  the migration only uses `DATABASE_URL`. Throwaway values satisfy pydantic;
  nothing connects to Discord/Gemini. Step 2's script doesn't import `config`,
  so it needs no stubs.

```bash
cd ~/snoopy_home
SHA=$(git rev-parse --short HEAD)
podman build -t localhost/snoopy-home:${SHA} .

kubectl -n data port-forward svc/postgres 5432:5432 &   # note the PID; kill when done
PG='postgresql://snoopy_rw:<STRONG_APP_PW>@localhost:5432/snoopy_home'

# 1. create the schema (as the owner role, so the app owns its tables)
podman run --rm --network=host -v ~/snoopy_home:/app:z -w /app \
  -e DATABASE_URL="$PG" -e DISCORD_TOKEN=x -e GEMINI_API_KEY=x \
  --entrypoint python localhost/snoopy-home:${SHA} -m storage.migrate

# 2. copy the data — prints per-table source/dest row counts, exits non-zero on any mismatch
podman run --rm --network=host -v ~/snoopy_home:/app:z -w /app \
  --entrypoint python localhost/snoopy-home:${SHA} \
  scripts/migrate_sqlite_to_pg.py --sqlite snoopy_home.db --pg "$PG"

kill %1    # stop the port-forward
```

If step 2 exits non-zero on a row-count mismatch, **stop** — do not deploy;
investigate before proceeding (the old bot is already stopped, so there's no
data drift, you can safely re-run).

## Gate 6 — first deploy

Point CI at the new box and cut a tag. First, in **GitHub → Settings → Secrets
and variables → Actions**, update `OCI_VM_HOST` to `<NEW_VM_IP>` (leave
`OCI_VM_USER=opc` and `OCI_SSH_PRIVATE_KEY` as-is).

Recommended: validate once **manually on the node** before trusting the tag path
— same commands CI runs, using the image you already built in Gate 5:

```bash
cd ~/snoopy_home
IMG=localhost/snoopy-home:${SHA}
# full path: sudo's secure_path excludes /usr/local/bin (so bare `sudo k3s`
# fails "command not found"), and it matches the NOPASSWD sudoers rule exactly
podman save "${IMG}" | sudo /usr/local/bin/k3s ctr images import -
kubectl apply -k deploy/k8s/overlays/prod
kubectl -n snoopy set image deploy/snoopy snoopy="${IMG}"
kubectl -n snoopy rollout status deploy/snoopy --timeout=120s
```

Then wire the automated path — from your laptop:

```bash
git tag v1.1.0 && git push origin v1.1.0
```

CI (`.github/workflows/deploy.yml`): tests → SSH to node → build → import →
`apply -k` → `set image` → `rollout status`.

## Gate 7 — verify

```bash
kubectl -n snoopy get pods                 # Running, READY 1/1
kubectl -n snoopy logs deploy/snoopy -f    # startup: migrations, Discord connect
kubectl -n snoopy port-forward deploy/snoopy 8080:8080 &   # then: curl localhost:8080/ready
```

Then talk to the **real** bot in Discord: mention it and confirm a reply; set a
reminder and confirm it fires; run a tools query ("what chores do we have?") to
confirm function calling works end-to-end. Also re-check the two-guild isolation
if the prod bot is in more than one server.

## Gate 8 — decommission the old box

Once prod has been stable for a bit and you've kept `snoopy_home.db` safe as the
rollback artifact:

```bash
ssh opc@<OLD_VM_IP>
# OL9: Quadlet units are generated from the .container file and auto-enabled, so
#      `systemctl --user disable` doesn't stick — remove the file instead:
rm ~/.config/containers/systemd/snoopy-home.container
systemctl --user daemon-reload
# OL8: systemctl --user disable --now snoopy-home
```

Then terminate the old instance in the OCI Console.

---

## Rollback

- **Bad image:** `kubectl -n snoopy rollout undo deploy/snoopy` (or `set image`
  back to the previous sha — still in containerd until `podman image prune`).
- **Bad data / catastrophic:** the old bot and its `snoopy_home.db` are intact
  until Gate 8. Re-start the old Quadlet bot (`systemctl --user start
  snoopy-home` on the old box) **after** scaling the new one to zero
  (`kubectl -n snoopy scale deploy/snoopy --replicas=0`) — never both at once.

## Everyday operations

| Task | Command (on node) |
|---|---|
| Live bot logs | `kubectl -n snoopy logs deploy/snoopy -f` |
| Status | `kubectl -n snoopy get pods` |
| Restart | `kubectl -n snoopy rollout restart deploy/snoopy` |
| Roll back | `kubectl -n snoopy rollout undo deploy/snoopy` |
| Shell in container | `kubectl -n snoopy exec -it deploy/snoopy -- sh` |
| Inspect the database | `kubectl -n data exec -it deploy/postgres -- psql -U snoopy_rw snoopy_home` |
