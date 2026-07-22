#!/usr/bin/env bash
# One-time setup on OCI VM (Oracle Linux 8 or 9). Run as opc.
# Podman is pre-installed on both — no extra install needed.
# Run AFTER creating ~/.env.snoopy (see deploy/env.snoopy.example).
set -euo pipefail

REPO_URL="${1:?Usage: $0 <github-repo-url> e.g. https://github.com/<YOUR_USERNAME>/<YOUR_REPO>.git}"
APP_DIR=~/snoopy_home
OL_VER=$(grep '^VERSION_ID=' /etc/os-release | cut -d= -f2 | tr -d '"' | cut -d. -f1)

if [[ ! -f ~/.env.snoopy ]]; then
    echo "ERROR: create ~/.env.snoopy first (see deploy/env.snoopy.example), then re-run."
    exit 1
fi
chmod 600 ~/.env.snoopy

echo "==> Detected Oracle Linux $OL_VER"

# ── 0. System dependencies ────────────────────────────────────────────────────
sudo dnf install -y git podman

# ── 1. Clone or update repo ───────────────────────────────────────────────────
if [[ -d "$APP_DIR/.git" ]]; then
    git -C "$APP_DIR" pull
else
    git clone "$REPO_URL" "$APP_DIR"
fi

# ── 2. Postgres — shared network + Quadlet container ─────────────────────────
# See docs/prod-provisioning.md #1. Skips cleanly if already provisioned by a
# prior run of this script.
podman network exists snoopy-net || podman network create snoopy-net

if [[ ! -f ~/.env.postgres ]]; then
    echo "ERROR: create ~/.env.postgres first (POSTGRES_PASSWORD=<superuser bootstrap password>, chmod 600), then re-run."
    exit 1
fi

mkdir -p ~/.config/containers/systemd
if [[ ! -f ~/.config/containers/systemd/snoopy-pg.container ]]; then
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
    systemctl --user daemon-reload
    systemctl --user start snoopy-pg
    echo "==> Waiting for Postgres to accept connections..."
    until podman exec snoopy-pg pg_isready -U postgres >/dev/null 2>&1; do sleep 1; done
fi

# App database + role, owned by the app role (sidesteps the PG15+ grant gotcha).
# `grep -c` guards against re-running this on a VM that already has the role.
if ! podman exec snoopy-pg psql -U postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname='snoopy_rw'" | grep -q 1; then
    if [[ ! -f ~/.env.snoopy ]] || ! grep -q '^DATABASE_URL=' ~/.env.snoopy; then
        echo "ERROR: set DATABASE_URL in ~/.env.snoopy first (see deploy/env.snoopy.example) so the app password here matches, then re-run."
        exit 1
    fi
    APP_PW=$(grep '^DATABASE_URL=' ~/.env.snoopy | sed -E 's#.*://snoopy_rw:([^@]+)@.*#\1#')
    podman exec -it snoopy-pg psql -U postgres -c \
        "CREATE ROLE snoopy_rw LOGIN PASSWORD '${APP_PW}';" -c \
        "CREATE DATABASE snoopy_home OWNER snoopy_rw;"
fi

# ── 3. Build image from local code ───────────────────────────────────────────
cd "$APP_DIR"
podman build -t snoopy-home:latest .

# ── 4. Register as a user systemd service ────────────────────────────────────
if [[ "$OL_VER" == "8" ]]; then
    # OL8: podman generate systemd (Podman < 4.4)
    podman run -d \
        --name snoopy-home \
        --network snoopy-net \
        --env-file ~/.env.snoopy \
        snoopy-home:latest

    mkdir -p ~/.config/systemd/user
    podman generate systemd --new --name snoopy-home \
        > ~/.config/systemd/user/snoopy-home.service

    podman stop snoopy-home && podman rm snoopy-home

elif [[ "$OL_VER" == "9" ]]; then
    # OL9: Quadlets (Podman 4.4+ native systemd integration, no container needed first)
    mkdir -p ~/.config/containers/systemd

    cat > ~/.config/containers/systemd/snoopy-home.container <<'EOF'
[Unit]
Description=snoopy-home Discord bot

[Container]
Image=localhost/snoopy-home:latest
Network=snoopy-net
EnvironmentFile=%h/.env.snoopy
Environment=PYTHONUNBUFFERED=1
PodmanArgs=--log-driver=journald

[Service]
Restart=always

[Install]
WantedBy=default.target
EOF

else
    echo "ERROR: unsupported Oracle Linux version: $OL_VER"
    exit 1
fi

# ── 5. Enable and start ───────────────────────────────────────────────────────
systemctl --user daemon-reload
if [[ "$OL_VER" == "8" ]]; then
    systemctl --user enable --now snoopy-home
elif [[ "$OL_VER" == "9" ]]; then
    # Quadlet units are auto-enabled via WantedBy=default.target — just start
    systemctl --user reset-failed snoopy-home 2>/dev/null || true
    systemctl --user start snoopy-home
fi

# ── 6. Linger: keeps user services alive after logout / on reboot ─────────────
loginctl enable-linger opc

echo ""
echo "==> Done. Check status with: systemctl --user status snoopy-home"
echo "==>         Logs:            sudo journalctl _SYSTEMD_USER_UNIT=snoopy-home.service -f"
