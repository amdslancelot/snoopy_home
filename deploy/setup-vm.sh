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

echo "==> Detected Oracle Linux $OL_VER"

# ── 1. Clone or update repo ───────────────────────────────────────────────────
if [[ -d "$APP_DIR/.git" ]]; then
    git -C "$APP_DIR" pull
else
    git clone "$REPO_URL" "$APP_DIR"
fi

# ── 2. Persistent volume for SQLite ──────────────────────────────────────────
podman volume exists snoopy-data || podman volume create snoopy-data

# ── 3. Build image from local code ───────────────────────────────────────────
cd "$APP_DIR"
podman build -t snoopy-home:latest .

# ── 4. Register as a user systemd service ────────────────────────────────────
if [[ "$OL_VER" == "8" ]]; then
    # OL8: podman generate systemd (Podman < 4.4)
    podman run -d \
        --name snoopy-home \
        -v snoopy-data:/data \
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
Volume=snoopy-data:/data
EnvironmentFile=%h/.env.snoopy

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
    systemctl --user start snoopy-home
fi

# ── 6. Linger: keeps user services alive after logout / on reboot ─────────────
loginctl enable-linger opc

echo ""
echo "==> Done. Check status with: systemctl --user status snoopy-home"
echo "==>         Logs:            journalctl --user -u snoopy-home -f"
