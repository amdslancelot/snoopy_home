# Deployment Guide — snoopy_home on OCI (Oracle Linux 8 / 9)

Target: OCI VM running Oracle Linux 8 or 9. Podman pre-installed (no Docker, no OCIR needed).
CI/CD: GitHub Actions tests → rsync code to VM → VM builds image locally → systemd restart.

| | OL8 | OL9 |
|---|---|---|
| Podman version | 4.x | 4.6+ |
| Systemd integration | `podman generate systemd` | Quadlets (`.container` file) |
| Unit file location | `~/.config/systemd/user/` | `~/.config/containers/systemd/` |
| Setup script | auto-detected | auto-detected |
| CI/CD deploy command | `systemctl --user restart snoopy-home` | same |

---

## Files

| File | Purpose |
|---|---|
| `Dockerfile` | Container image (Python 3.11, voice deps) |
| `entrypoint.sh` | Decodes `GOOGLE_SA_JSON_B64` → file, then runs `main.py` |
| `.dockerignore` | Excludes secrets, DB, tests from image build context |
| `.github/workflows/deploy.yml` | CI tests + rsync + build + restart |
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

Three secrets total — that's it.

---

## Part 3 — First-time VM setup

### 3a. SSH into VM and create secrets file

```bash
ssh opc@<YOUR_VM_PUBLIC_IP>
```

Copy the example and fill in values:

```bash
# Copy deploy/env.snoopy.example from your local machine
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

### 3b. Run setup script

From your local machine (pass your GitHub repo URL — public repo shown; for private repo add your token):

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

**OL8** — generates systemd unit from a running container (`podman generate systemd --new`), writes to `~/.config/systemd/user/snoopy-home.service`.

**OL9** — writes a Quadlet file to `~/.config/containers/systemd/snoopy-home.container`; no need to start a container first.

Both paths then run `systemctl --user enable --now snoopy-home` and `loginctl enable-linger opc`.

### 3c. Verify

```bash
systemctl --user status snoopy-home
journalctl --user -u snoopy-home -f
```

---

## Part 4 — CI/CD pipeline

Push to `main` triggers:

```
push to main
  → test        unit + SQLite integration tests (GitHub-hosted runner, no API calls)
  → deploy      SSH into VM
                → git pull (authenticated via secrets.GITHUB_TOKEN, no extra secret needed)
                → podman build -t snoopy-home:latest .
                → systemctl --user restart snoopy-home
                → podman image prune -f
```

PRs and non-main branches run `test` only — no deploy.

---

## Everyday operations

| Task | Command (on VM) |
|---|---|
| Live logs | `journalctl --user -u snoopy-home -f` |
| Restart | `systemctl --user restart snoopy-home` |
| Stop | `systemctl --user stop snoopy-home` |
| Shell into container | `podman exec -it snoopy-home sh` |
| Inspect SQLite | `podman run --rm -v snoopy-data:/data alpine sqlite3 /data/snoopy_home.db` |
| Rebuild manually | `cd ~/snoopy_home && podman build -t snoopy-home:latest . && systemctl --user restart snoopy-home` |

---

## Secrets never in git

- `.env` — local dev secrets (gitignored)
- `~/.env.snoopy` — production secrets, lives only on VM
- `*.json` — service account key (gitignored)
- `GOOGLE_SA_JSON_B64` — base64 of service account JSON, set in `~/.env.snoopy` on VM

---

## Architecture notes

- **No OCIR, no registry** — image built locally on VM from rsync'd source.
- **No Docker daemon** — Podman is daemonless; each container is a direct child process of systemd.
- **Restart policy** — handled by `systemctl --user enable` + `loginctl enable-linger opc`.
- **SQLite persistence** — `snoopy-data` Podman volume, mounted at `/data` inside container.
- **Google SA JSON** — `entrypoint.sh` decodes `GOOGLE_SA_JSON_B64` env var to `/app/service_account.json` at container start; no file mount needed.
