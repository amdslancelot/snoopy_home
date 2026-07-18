# Deployment Guide — snoopy_home on OCI (Oracle Linux 8 / 9)

> **Note:** this guide covers the current single-environment Podman/Quadlet deployment. The plan for moving to **staging + prod** on the single-node k3s cluster is in [DEPLOY-K3S.md](DEPLOY-K3S.md).

Target: OCI VM running Oracle Linux 8 or 9. No Docker, no OCIR needed — `setup-vm.sh` installs Podman via `dnf`.
CI/CD: GitHub Actions tests → SSH into VM → git pull → build image locally → systemd restart.

| | OL8 | OL9 |
|---|---|---|
| Podman version | 4.x | 4.6+ (tested: 5.8.2, installed via `dnf`) |
| Systemd integration | `podman generate systemd` | Quadlets (`.container` file) |
| Unit file location | `~/.config/systemd/user/` | `~/.config/containers/systemd/` |
| Container name | `snoopy-home` | `systemd-snoopy-home` |
| Setup script | auto-detected | auto-detected |
| CI/CD deploy command | `systemctl --user restart snoopy-home` | same |

---

## Files

| File | Purpose |
|---|---|
| `Dockerfile` | Container image (Python 3.11, voice deps) |
| `entrypoint.sh` | Decodes `GOOGLE_SA_JSON_B64` → file, then runs `main.py`; degrades gracefully if decode fails |
| `.dockerignore` | Excludes secrets, DB, tests from image build context |
| `.github/workflows/deploy.yml` | CI tests + git pull + build + restart |
| `deploy/setup-vm.sh` | One-time VM setup |
| `deploy/env.snoopy.example` | Secrets template for the VM |

---

## Part 1 — OCI Console (one-time)

1. Console → **Compute → Instances → Create Instance**
2. Image: **Oracle Linux 8** or **Oracle Linux 9**
3. Shape: `VM.Standard.A1.Flex` — 1 OCPU, 6 GB RAM (Always Free)
4. Assign a **public IP**
5. Upload your SSH public key
6. Note the **public IP**

No container registry needed — image is built directly on the VM.

---

## Part 2 — GitHub Actions secrets

Repo → **Settings → Secrets and variables → Actions → New repository secret**

| Secret | Value |
|---|---|
| `OCI_VM_HOST` | `<VM_PUBLIC_IP>` |
| `OCI_VM_USER` | `opc` |
| `OCI_SSH_PRIVATE_KEY` | Full content of deploy private key (`cat ~/.ssh/oci_deploy_key`) |

Three secrets total. `GITHUB_TOKEN` is auto-provided by GitHub Actions — no extra secret needed for git pull.

---

## Part 3 — First-time VM setup

### 3a. SSH into VM and create secrets file

```bash
scp deploy/env.snoopy.example opc@<YOUR_VM_PUBLIC_IP>:~/.env.snoopy
ssh opc@<YOUR_VM_PUBLIC_IP>
nano ~/.env.snoopy
```

Fill in at minimum:
- `DISCORD_TOKEN`
- `GEMINI_API_KEY`
- `DB_PATH=/data/snoopy_home.db`

For Google Calendar — encode service account JSON as base64:

```bash
# macOS
base64 -i <YOUR_SERVICE_ACCOUNT_JSON_FILE> | tr -d '\n'

# Linux
base64 -w 0 <YOUR_SERVICE_ACCOUNT_JSON_FILE>
```

Paste output as `GOOGLE_SA_JSON_B64=<BASE64_OUTPUT>` in `~/.env.snoopy`.
If Google Calendar is not needed, leave `GOOGLE_SA_JSON_B64` commented out.

### 3b. Install git (OL9 does not ship with git)

```bash
ssh opc@<YOUR_VM_PUBLIC_IP> "sudo dnf install -y git"
```

### 3c. Run setup script

```bash
ssh opc@<YOUR_VM_PUBLIC_IP> bash -s -- https://github.com/<YOUR_USERNAME>/<YOUR_REPO>.git \
  < deploy/setup-vm.sh
```

For a **private repo**, embed a Personal Access Token (PAT) with `repo` read scope:

```bash
ssh opc@<YOUR_VM_PUBLIC_IP> bash -s -- https://<YOUR_GITHUB_USERNAME>:<YOUR_PAT>@github.com/<YOUR_USERNAME>/<YOUR_REPO>.git \
  < deploy/setup-vm.sh
```

The setup script auto-detects OL version:

**OL8** — generates systemd unit from a running container (`podman generate systemd --new`), writes to `~/.config/systemd/user/snoopy-home.service`. Runs `systemctl --user enable --now snoopy-home`.

**OL9** — writes a Quadlet file to `~/.config/containers/systemd/snoopy-home.container`; no container needed first. Runs `systemctl --user start snoopy-home` (Quadlets are auto-enabled via `WantedBy=default.target`).

Both paths run `loginctl enable-linger opc` so the service survives logout and reboots.

### 3d. Verify

```bash
ssh opc@<YOUR_VM_PUBLIC_IP> "systemctl --user status snoopy-home --no-pager"
```

---

## Part 4 — CI/CD pipeline

Push to `main` triggers:

```
push to main
  → test        unit + SQLite integration tests (GitHub-hosted runner, no API calls)
  → deploy      SSH into VM
                → git pull (authenticated via secrets.GITHUB_TOKEN)
                → podman build -t snoopy-home:latest .
                → systemctl --user restart snoopy-home
                → podman image prune -f
```

PRs and non-main branches run `test` only — no deploy.

---

## Everyday operations

| Task | Command (on VM) |
|---|---|
| Live bot logs | `sudo journalctl CONTAINER_NAME=systemd-snoopy-home -f` |
| Last 50 bot log lines | `sudo journalctl CONTAINER_NAME=systemd-snoopy-home -n 50 --no-pager` |
| Lifecycle events (start/stop) | `sudo journalctl _SYSTEMD_USER_UNIT=snoopy-home.service -n 20 --no-pager` |
| Service status | `systemctl --user status snoopy-home --no-pager` |
| Restart | `systemctl --user restart snoopy-home` |
| Stop | `systemctl --user stop snoopy-home` |
| Shell into container (OL9) | `podman exec -it systemd-snoopy-home sh` |
| Shell into container (OL8) | `podman exec -it snoopy-home sh` |
| Run bot manually (debug) | `podman run --rm -it -v snoopy-data:/data --env-file ~/.env.snoopy -e PYTHONUNBUFFERED=1 localhost/snoopy-home:latest` |
| Rebuild manually | `cd ~/snoopy_home && git pull && podman build -t snoopy-home:latest . && systemctl --user restart snoopy-home` |

---

## SQLite database

DB lives in the `snoopy-data` Podman volume:

```
/home/opc/.local/share/containers/storage/volumes/snoopy-data/_data/snoopy_home.db
```

Inspect live:
```bash
sqlite3 /home/opc/.local/share/containers/storage/volumes/snoopy-data/_data/snoopy_home.db
```

Replace with a local DB (stop bot first):
```bash
systemctl --user stop snoopy-home
scp snoopy_home.db opc@<YOUR_VM_PUBLIC_IP>:/home/opc/.local/share/containers/storage/volumes/snoopy-data/_data/snoopy_home.db
ssh opc@<YOUR_VM_PUBLIC_IP> "systemctl --user start snoopy-home"
```

---

## Debugging

**1. Live logs (service running normally)**
```bash
sudo journalctl CONTAINER_NAME=systemd-snoopy-home -f
```

Two journal filters exist — use the right one:

| Filter | Shows |
|---|---|
| `CONTAINER_NAME=systemd-snoopy-home` | Python app stdout/stderr (bot logs) |
| `_SYSTEMD_USER_UNIT=snoopy-home.service` | Podman lifecycle events (start/stop/die) |

Note: `journalctl --user` does NOT work on OL9 — Podman Quadlets write to the system journal. Always use `sudo journalctl`.

`Environment=PYTHONUNBUFFERED=1` in the Quadlet file is required — without it Python buffers stdout and logs never reach journald.

**2. Debug mode — foreground with live output**

Stop the service, then run the container interactively. All stdout/stderr prints directly to your terminal in real time.

```bash
systemctl --user stop snoopy-home

podman run --rm -it \
  -v snoopy-data:/data \
  --env-file ~/.env.snoopy \
  -e PYTHONUNBUFFERED=1 \
  localhost/snoopy-home:latest
```

- `--rm -it` — foreground, terminal attached, Ctrl+C to stop
- `-e PYTHONUNBUFFERED=1` — forces Python to flush logs immediately instead of buffering

Restore service when done:
```bash
systemctl --user start snoopy-home
```

**3. Bypass entrypoint, run Python directly**

Skips `entrypoint.sh` (base64 decode, Google SA setup) and runs `main.py` directly. Useful when `entrypoint.sh` itself is the failure point.

```bash
systemctl --user stop snoopy-home

podman run --rm -it \
  -v snoopy-data:/data \
  --env-file ~/.env.snoopy \
  -e PYTHONUNBUFFERED=1 \
  --entrypoint python \
  localhost/snoopy-home:latest -u main.py
```

**4. Shell into running container**
```bash
podman exec -it systemd-snoopy-home sh   # OL9
podman exec -it snoopy-home sh           # OL8
```

**5. Verify Google SA JSON base64 is valid**
```bash
python3 -c "
import base64, json
for line in open('/home/opc/.env.snoopy'):
    if line.startswith('GOOGLE_SA_JSON_B64='):
        val = line.split('=',1)[1].strip()
        try:
            json.loads(base64.b64decode(val))
            print('OK')
        except Exception as e:
            print('FAIL:', e)
"
```

**6. Restart and watch logs**
```bash
systemctl --user reset-failed snoopy-home 2>/dev/null; \
systemctl --user restart snoopy-home && \
sudo journalctl CONTAINER_NAME=systemd-snoopy-home -f
```

---

## Secrets never in git

- `.env` — local dev secrets (gitignored)
- `~/.env.snoopy` — production secrets, lives only on VM
- `*.json` — service account key (gitignored)
- `GOOGLE_SA_JSON_B64` — base64 of service account JSON, set in `~/.env.snoopy` on VM

---

## Architecture notes

- **No OCIR, no registry** — image built locally on VM from git-pulled source.
- **No Docker daemon** — Podman is daemonless; each container is a direct child process of systemd.
- **Restart policy** — handled by `systemctl --user enable` (OL8) / `WantedBy=default.target` (OL9) + `loginctl enable-linger opc`.
- **SQLite persistence** — `snoopy-data` Podman volume at `/home/opc/.local/share/containers/storage/volumes/snoopy-data/_data/`, mounted at `/data` inside container.
- **Google SA JSON** — `entrypoint.sh` decodes `GOOGLE_SA_JSON_B64` to `/app/service_account.json` at container start; degrades gracefully if decode fails (bot starts without Calendar).
- **OL9 container name** — Quadlet prefixes container name with `systemd-`, so it becomes `systemd-snoopy-home` (not `snoopy-home`).
