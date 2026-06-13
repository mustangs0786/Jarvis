#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# setup.sh — provision a fresh Azure Ubuntu 22.04 VM to run the auto-apply agent.
# Run this ONCE on the VM (after `git clone` + `scp` of .env and browser_profile).
#   ssh azureuser@<vm-fqdn>
#   bash ~/Resume_Builder/deploy/setup.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

APP_DIR="$HOME/Jarvis"

echo "==> System packages (Xvfb gives headful Chromium a virtual display; Caddy = HTTPS)"
sudo apt-get update
sudo apt-get install -y xvfb git curl debian-keyring debian-archive-keyring apt-transport-https

# Caddy (auto-HTTPS reverse proxy)
if ! command -v caddy >/dev/null 2>&1; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
  sudo apt-get update && sudo apt-get install -y caddy
fi

echo "==> uv (Python package manager)"
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"

echo "==> Python deps + Chromium with all system libs"
cd "$APP_DIR"
uv sync
uv run playwright install --with-deps chromium

echo "==> systemd service (keeps the app up + auto-restart for 30 days)"
sudo cp "$APP_DIR/deploy/resume-apply.service" /etc/systemd/system/resume-apply.service
sudo systemctl daemon-reload
sudo systemctl enable --now resume-apply.service

echo "==> Caddy reverse proxy (HTTPS on your Azure FQDN)"
sudo cp "$APP_DIR/deploy/Caddyfile" /etc/caddy/Caddyfile
echo "    ! EDIT /etc/caddy/Caddyfile and replace YOUR_FQDN, then: sudo systemctl reload caddy"

echo
echo "Done. Check status:"
echo "  systemctl status resume-apply.service   # app"
echo "  journalctl -u resume-apply -f            # live logs"
