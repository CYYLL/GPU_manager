#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# GPU Resource Manager v2 — MAINTENANCE-WINDOW cutover script
#
# Switches the machine from the v1 install (/amax/gpu_manager) to v2
# (/amax/gpu_manager_v2) ATOMICALLY. It STOPS the live v1 backend — there is a
# service interruption. Run only during a maintenance window, as root:
#
#     sudo bash deploy/gpu-manager-v2-cutover.sh          # interactive
#     sudo bash deploy/gpu-manager-v2-cutover.sh --yes    # skip confirmations
#
#  What it does (each step idempotent):
#    1. Preflight  — v2 frontend build exists, v2 .env exists, nginx present.
#    2. Backup     — current systemd units + nginx site → deploy/backup/<ts>/.
#    3. Stop v1    — disable+stop gpu-manager-backend/frontend if active.
#    4. Install v2 — copy v2 units, enable+start backend (:8000) & frontend (:3001).
#    5. Switch nginx root/proxy to v2, reload after `nginx -t`.
#    6. Verify     — unit states + HTTP checks.
#
#  ⚠ DATA: v2 runs on its OWN fresh SQLite DB (backend/gpu_resource_manager.db).
#    v1 users/containers are NOT migrated — an empty v2 DB starts with no users.
#    The v1 code+DB at /amax/gpu_manager are left untouched (rollback = re-run
#    backups from deploy/backup and re-enable the v1 units).
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$SCRIPT_DIR"
V2_ROOT="$(dirname "$DEPLOY_DIR")"          # /amax/gpu_manager_v2
BACKEND_DIR="$V2_ROOT/backend"
FRONTEND_BUILD="$V2_ROOT/frontend/build"
ENV_FILE="$V2_ROOT/.env"
BACKUP_DIR="$DEPLOY_DIR/backup/$(date +%Y%m%d-%H%M%S)"

V1_SERVICES=(gpu-manager-backend gpu-manager-frontend)
# v1 / v2 share these unit names; we stop the services but keep backups so
# rollback is trivial.

confirm() {  # usage: confirm "msg"  → abort unless --yes or y
  if [ "${1:-}" != "--yes" ]; then
    echo ""
    echo "This STOPS the running v1 GPU Manager (service interruption)."
    read -r -p "Proceed? [y/N] " ans
    if [ "$ans" != "y" ] && [ "$ans" != "Y" ]; then
      echo "Aborted."; exit 1
    fi
  fi
}

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: run as root (sudo)." >&2; exit 1
  fi
}

preflight() {
  [ -d "$FRONTEND_BUILD" ] || { echo "ERROR: v2 frontend build missing: $FRONTEND_BUILD" >&2
    echo "       Run: (cd $V2_ROOT/frontend && npm run build)" >&2; exit 1; }
  [ -f "$ENV_FILE" ] || { echo "ERROR: v2 .env missing: $ENV_FILE" >&2; exit 1; }
  if grep -q '^SECRET_KEY=you-need-to-change\|^SECRET_KEY=your-secret-key-change\|^SECRET_KEY=$' "$ENV_FILE"; then
    echo "WARNING: .env still has the placeholder SECRET_KEY." >&2
  fi
  command -v nginx >/dev/null || { echo "ERROR: nginx not installed." >&2; exit 1; }
  [ -d "$BACKEND_DIR" ] || { echo "ERROR: backend dir missing: $BACKEND_DIR" >&2; exit 1; }
}

backup() {
  mkdir -p "$BACKUP_DIR"
  for s in "${V1_SERVICES[@]}"; do
    [ -f "/etc/systemd/system/$s.service" ] && cp "/etc/systemd/system/$s.service" "$BACKUP_DIR/"
  done
  [ -f "/etc/nginx/sites-available/gpu-manager" ] && cp "/etc/nginx/sites-available/gpu-manager" "$BACKUP_DIR/"
  echo "==> Backups -> $BACKUP_DIR"
}

stop_v1() {
  for s in "${V1_SERVICES[@]}"; do
    if systemctl list-unit-files "$s.service" >/dev/null 2>&1 && systemctl is-active "$s" >/dev/null 2>&1; then
      echo "==> Stopping & disabling v1 $s..."
      systemctl disable --now "$s" || true
    else
      echo "==> $s not active — nothing to stop."
    fi
  done
}

install_v2() {
  echo "==> Installing v2 backend & frontend units..."
  cp "$DEPLOY_DIR/gpu-manager-backend.service" /etc/systemd/system/
  cp "$DEPLOY_DIR/gpu-manager-frontend.service" /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable gpu-manager-backend
  systemctl start gpu-manager-backend
  systemctl enable gpu-manager-frontend
  systemctl start gpu-manager-frontend
}

switch_nginx() {
  echo "==> Switching nginx to v2..."
  cp "$DEPLOY_DIR/gpu-manager.nginx.conf" /etc/nginx/sites-available/gpu-manager
  ln -sf /etc/nginx/sites-available/gpu-manager /etc/nginx/sites-enabled/gpu-manager
  rm -f /etc/nginx/sites-enabled/default
  nginx -t && systemctl reload nginx
}

verify() {
  echo ""
  echo "==> Service states:"
  echo "  backend : $(systemctl is-active gpu-manager-backend)  ($(systemctl is-enabled gpu-manager-backend))"
  echo "  frontend: $(systemctl is-active gpu-manager-frontend)  ($(systemctl is-enabled gpu-manager-frontend))"
  echo "  nginx   : $(systemctl is-active nginx)"
  echo "==> HTTP checks:"
  curl -fsS -o /dev/null -w '  /docs          → HTTP %{http_code}\n' http://127.0.0.1:8000/docs || echo "  /docs          → FAILED"
  curl -fsS -o /dev/null -w '  / (SPA)        → HTTP %{http_code}\n' http://127.0.0.1/ || echo "  / (SPA)        → FAILED"
  echo ""
  echo "==> Next:"
  echo "  sudo journalctl -u gpu-manager-backend -f"
  echo "  Register an admin user on the new v2 DB: POST /api/users/register then promote role."
  echo "  Rollback: re-copy $BACKUP_DIR/* to /etc/systemd/system + nginx, then enable the v1 units."
}

FLAG="${1:-}"
require_root
confirm "$FLAG"
preflight
echo "==> v2 root: $V2_ROOT"
backup
stop_v1
install_v2
switch_nginx
verify
