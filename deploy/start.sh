#!/usr/bin/env bash
# (Re)start the Recal backend in the background with nohup. Writes NO log files:
# output goes to /dev/null and the app's own file logging is off (LOG_TO_FILE=0).
#
#   bash deploy/start.sh        # restart in the background (survives closing SSH)
#   bash deploy/start.sh fg     # debug: run in this terminal, logs on screen only
#   bash deploy/start.sh stop   # stop it
set -euo pipefail
cd "$(dirname "$0")/.."

MATCH="uvicorn backend.main:app"
CMD=(.venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port 8000 --workers 1
     --proxy-headers --forwarded-allow-ips 127.0.0.1)

pkill -f "$MATCH" 2>/dev/null || true
for _ in $(seq 1 10); do pgrep -f "$MATCH" >/dev/null || break; sleep 1; done

case "${1:-}" in
  stop) echo "backend stopped" ;;
  fg)   LOG_TO_FILE=0 exec "${CMD[@]}" ;;
  *)    LOG_TO_FILE=0 PYTHONUNBUFFERED=1 setsid nohup "${CMD[@]}" >/dev/null 2>&1 </dev/null &
        echo "backend started in the background (pid $!)" ;;
esac
