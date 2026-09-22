#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# GPU Resource Manager v2 — one-click service installer
#
#     sudo bash install-services.sh           # fresh v2 host, or after cutover
#     sudo bash install-services.sh --cutover # replace a LIVE v1 install now
#                                             # (== sudo bash gpu-manager-v2-cutover.sh)
#
# SAFETY: if this machine is currently running the v1 backend
# (/amax/gpu_manager on :8000), the default run REFUSES to touch it — it would
# otherwise overwrite the live v1 unit file and leave a confusing half-cutover
# state. Pass --cutover (or run gpu-manager-v2-cutover.sh) during a maintenance
# window to stop v1 first. See docs/deployment.md §部署.
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
V2_ROOT="$(dirname "$SCRIPT_DIR")"
FRONTEND_BUILD="$V2_ROOT/frontend/build"
ENV_FILE="$V2_ROOT/.env"

FLAG="${1:-}"

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: run as root (sudo)." >&2; exit 1
  fi
}

preflight() {
  [ -d "$FRONTEND_BUILD" ] || { echo "ERROR: v2 frontend build missing: $FRONTEND_BUILD" >&2
    echo "       Run: (cd $V2_ROOT/frontend && npm run build)" >&2; exit 1; }
  [ -f "$ENV_FILE" ] || { echo "ERROR: v2 .env missing: $ENV_FILE" >&2; exit 1; }
  command -v nginx >/dev/null || { echo "WARNING: nginx not installed — install then re-run, or skip nginx." >&2; }
}

# A live v1 install is detected by: the installed backend unit still pointing at
# /amax/gpu_manager/backend, or any process already listening on :8000 that is
# NOT our own v2 unit (post-cutover re-runs are safe).
v1_live() {
  if systemctl is-active gpu-manager-backend >/dev/null 2>&1 \
     && grep -q '/amax/gpu_manager/backend' /etc/systemd/system/gpu-manager-backend.service 2>/dev/null; then
    return 0
  fi
  if ss -ltn 2>/dev/null | grep -q ':8000 '; then
    if [ -f /etc/systemd/system/gpu-manager-backend.service ] \
       && grep -q '/amax/gpu_manager_v2/backend' /etc/systemd/system/gpu-manager-backend.service 2>/dev/null; then
      return 1   # :8000 held by our own v2 unit → re-run safe
    fi
    return 0
  fi
  return 1
}

install_v2_now() {
  echo "==> Installing v2 backend & frontend units..."
  cp "$SCRIPT_DIR/gpu-manager-backend.service" /etc/systemd/system/
  cp "$SCRIPT_DIR/gpu-manager-frontend.service" /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable gpu-manager-backend && systemctl restart gpu-manager-backend
  echo "    backend: $(systemctl is-active gpu-manager-backend)"
  systemctl enable gpu-manager-frontend && systemctl start gpu-manager-frontend
  echo "    frontend: $(systemctl is-active gpu-manager-frontend)"

  echo "==> Configuring nginx..."
  if command -v nginx >/dev/null; then
    cp "$SCRIPT_DIR/gpu-manager.nginx.conf" /etc/nginx/sites-available/gpu-manager
    ln -sf /etc/nginx/sites-available/gpu-manager /etc/nginx/sites-enabled/
    rm -f /etc/nginx/sites-enabled/default
    nginx -t && systemctl reload nginx
    echo "    Nginx configured and reloaded."
  fi
}

verify() {
  echo ""
  echo "==> Status:"
  systemctl is-active gpu-manager-backend gpu-manager-frontend nginx --no-pager 2>/dev/null || true
  echo "==> Logs:"
  echo "  sudo journalctl -u gpu-manager-backend -f"
  echo "  sudo journalctl -u gpu-manager-frontend -f"
}

require_root
preflight

if v1_live; then
  echo ""
  echo "!! A v1 GPU Manager install is live on :8000 (/amax/gpu_manager)."
  if [ "$FLAG" = "--cutover" ]; then
    echo "==> --cutover: switching now (stops v1)."
    exec bash "$SCRIPT_DIR/gpu-manager-v2-cutover.sh" --yes
  else
    echo "    This installer refuses to overwrite the running v1 config."
    echo ""
    echo "    During a MAINTENANCE WINDOW run either:"
    echo "      sudo bash $SCRIPT_DIR/gpu-manager-v2-cutover.sh        # interactive"
    echo "      sudo bash $SCRIPT_DIR/install-services.sh --cutover    # same, --yes"
    echo ""
    echo "    (v2 uses its own fresh SQLite DB — v1 users/data are NOT migrated.)"
    exit 1
  fi
fi

echo "==> No live v1 conflict — installing v2."
install_v2_now
verify
