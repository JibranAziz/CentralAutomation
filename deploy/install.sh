#!/usr/bin/env bash
# One-shot installer for Aruba Central Automation on Debian/Ubuntu with a
# self-signed TLS certificate. Run from the repo root as root:
#
#   sudo ./deploy/install.sh central.example.lan
#   sudo ./deploy/install.sh 192.168.1.50
#   sudo SERVER_NAME="central.lan 192.168.1.50" ./deploy/install.sh
#
# It: creates a venv + installs deps, generates a self-signed cert (SANs for
# every name/IP you pass), installs the systemd service and the nginx vhost,
# and starts everything. Re-running is safe (it refreshes in place).
#
# Needs: python3, python3-venv, nginx, openssl (apt install -y ...).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="${APP_DIR:-/opt/aruba-central-automation}"
RUN_USER="${RUN_USER:-aruba-ca}"
SERVER_NAME="${SERVER_NAME:-${*:-}}"
SVC=aruba-central-automation

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
[ -n "$SERVER_NAME" ] || { echo "usage: $0 <hostname-or-ip> [more ...]" >&2; exit 1; }
primary="${SERVER_NAME%% *}"

echo ">> user $RUN_USER"
id "$RUN_USER" &>/dev/null || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$RUN_USER"

echo ">> code -> $APP_DIR"
mkdir -p "$APP_DIR"
if [ "$(realpath "$REPO_DIR")" != "$(realpath "$APP_DIR")" ]; then
    rsync -a --delete --exclude .git --exclude .venv "$REPO_DIR/" "$APP_DIR/"
fi

echo ">> venv"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
chown -R "$RUN_USER:$RUN_USER" "$APP_DIR"

echo ">> self-signed cert for: $SERVER_NAME"
# shellcheck disable=SC2086
bash "$APP_DIR/deploy/gen-cert.sh" $SERVER_NAME

echo ">> systemd unit"
sed -e "s#__USER__#$RUN_USER#g" -e "s#__APP_DIR__#$APP_DIR#g" \
    "$APP_DIR/deploy/aruba-central-automation.service" > "/etc/systemd/system/$SVC.service"
systemctl daemon-reload
systemctl enable --now "$SVC"
systemctl restart "$SVC"

echo ">> nginx vhost"
sed "s#__SERVER_NAME__#$primary#g" "$APP_DIR/deploy/nginx.conf" \
    > /etc/nginx/sites-available/$SVC
ln -sf /etc/nginx/sites-available/$SVC /etc/nginx/sites-enabled/$SVC
nginx -t
systemctl reload nginx

echo
echo "Done. Open  https://$primary/  (accept the self-signed warning once)."
curl -sk -o /dev/null -w "local health check: %{http_code}\n" https://127.0.0.1/healthz || true
