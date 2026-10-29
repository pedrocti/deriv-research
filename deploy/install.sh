#!/usr/bin/env bash
# Installs or updates the Deriv research collector. Run as root. Safe to re-run.
# Touches nothing outside /opt/dresearch, /var/lib/dresearch, /etc/systemd/system/dresearch-*
# and /usr/local/bin/dresearch. Installs no system packages (Ubuntu 25.04 repos are end-of-life).
set -euo pipefail
REPO_URL="${REPO_URL:-https://github.com/pedrocti/deriv-research.git}"
APP=/opt/dresearch
DATA=/var/lib/dresearch
SVC_USER=dresearch

[ "$(id -u)" = 0 ] || { echo "ERROR: run as root"; exit 1; }
command -v git >/dev/null || { echo "ERROR: git is not installed"; exit 1; }
python3 -c 'import sys, venv; assert sys.version_info >= (3, 10)' \
  || { echo "ERROR: need Python 3.10+ with venv"; exit 1; }

echo "[1/6] service user"
id -u "$SVC_USER" >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin "$SVC_USER"
install -d -m 750 -o "$SVC_USER" -g "$SVC_USER" "$DATA"

echo "[2/6] code"
if [ -d "$APP/src/.git" ]; then
  git -C "$APP/src" fetch -q origin && git -C "$APP/src" reset -q --hard origin/main
else
  install -d "$APP" && git clone -q "$REPO_URL" "$APP/src"
fi
echo "      version: $(git -C "$APP/src" log -1 --format='%h %cd' --date=short)"

echo "[3/6] python environment (pinned versions)"
[ -x "$APP/venv/bin/python" ] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q -r "$APP/src/requirements.lock"
"$APP/venv/bin/pip" install -q --no-deps --force-reinstall "$APP/src"

echo "[4/6] systemd units"
install -m 644 "$APP/src/deploy/dresearch-collector.service" "$APP/src/deploy/dresearch-audit.service" \
  "$APP/src/deploy/dresearch-audit.timer" "$APP/src/deploy/dresearch-backfill.service" "$APP/src/deploy/dresearch-dashboard.service" /etc/systemd/system/
systemctl daemon-reload

echo "[5/6] 'dresearch' command"
cat > /usr/local/bin/dresearch <<WRAP
#!/bin/sh
exec runuser -u $SVC_USER -- env DRESEARCH_DATA=$DATA $APP/venv/bin/dresearch "\$@"
WRAP
chmod 755 /usr/local/bin/dresearch

echo "[6/6] restart collector if it was running"
systemctl try-restart dresearch-collector.service dresearch-dashboard.service || true
echo "INSTALL OK"
