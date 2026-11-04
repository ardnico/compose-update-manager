#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "\${BASH_SOURCE[0]}")" && pwd)"
ENABLE_TIMER=false
DRY_RUN=false

usage() {
  cat <<'EOF'
Usage: sudo bash install.sh [--enable-timer] [--dry-run]
Installs to /opt/svc-update and /usr/local/bin/svc-update.
Preserves existing config.toml. Stops timer unless explicitly enabled.
EOF
}
while (($#)); do
  case "$1" in
    --enable-timer) ENABLE_TIMER=true ;;
    --dry-run) DRY_RUN=true ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'Unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done
for file in svc_update.py config.example.toml systemd/svc-update.service systemd/svc-update.timer bin/svc-update; do
  [[ -f "$ROOT_DIR/$file" ]] || { echo "Missing source file: $file" >&2; exit 1; }
done

if "$DRY_RUN"; then
  echo 'DRY RUN: install binary, CLI, systemd units, preserve config, disable timer.'
  echo "DRY RUN: --enable-timer=$ENABLE_TIMER"
  exit 0
fi
[[ $EUID -eq 0 ]] || { echo 'Run as root: sudo bash install.sh' >&2; exit 1; }
command -v python3 >/dev/null || { echo 'Python 3 missing' >&2; exit 1; }
python3 - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit('Python 3.11+ required')
PY
command -v docker >/dev/null || { echo 'Docker CLI missing' >&2; exit 1; }
docker compose version >/dev/null || { echo 'Docker Compose v2 missing' >&2; exit 1; }
command -v systemctl >/dev/null || { echo 'systemd missing' >&2; exit 1; }
if systemctl is-active --quiet svc-update.service; then
  echo 'Updater is running; refuse replacing executable during transaction' >&2
  exit 1
fi
systemctl disable --now svc-update.timer >/dev/null 2>&1 || true
install -d -m 0755 /opt/svc-update /usr/local/bin
install -d -m 0700 /etc/svc-update /var/backups/svc-update
if [[ -f /opt/svc-update/svc_update.py ]]; then
  backup="/opt/svc-update/svc_update.py.bak.$(date -u +%Y%m%dT%H%M%SZ)"
  cp -p /opt/svc-update/svc_update.py "$backup"
  chmod 0600 "$backup"
  echo "Previous program backed up: $backup"
fi
install -m 0755 "$ROOT_DIR/svc_update.py" /opt/svc-update/svc_update.py
install -m 0755 "$ROOT_DIR/bin/svc-update" /usr/local/bin/svc-update
if [[ ! -e /etc/svc-update/config.toml ]]; then
  install -m 0600 "$ROOT_DIR/config.example.toml" /etc/svc-update/config.toml
  echo 'Created disabled config.toml from example. Edit before enabling.'
else
  echo 'Preserved existing /etc/svc-update/config.toml'
fi
install -m 0644 "$ROOT_DIR/systemd/svc-update.service" /etc/systemd/system/svc-update.service
install -m 0644 "$ROOT_DIR/systemd/svc-update.timer" /etc/systemd/system/svc-update.timer
systemctl daemon-reload
if "$ENABLE_TIMER"; then
  /usr/local/bin/svc-update status || { echo 'Config validation failed; timer disabled' >&2; exit 1; }
  systemctl enable --now svc-update.timer
  echo 'WARNING: Timer enabled. Validate backup/restore first.'
else
  echo 'Timer disabled; when ready: sudo systemctl enable --now svc-update.timer'
fi
echo 'Installed. Verify with: sudo svc-update status; sudo svc-update check nginx'
