#!/usr/bin/env bash
# Desktop development helper: run the web service and the windowed player against .dev.
#   setup/dev.sh start | stop | restart | status
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PITV_DATA="$ROOT/.dev" PITV_RUN="$ROOT/.dev/run" PITV_WINDOWED=1
export PITV_MPV_ARGS="${PITV_MPV_ARGS:---geometry=768x576}"   # 4:3 window like the real set
PORT="${PORT:-8080}"
mkdir -p "$PITV_RUN"

stop_one() {  # name
  local f="$PITV_RUN/$1.pid"
  if [ -f "$f" ] && kill -0 "$(cat "$f")" 2>/dev/null; then kill "$(cat "$f")"; sleep 1; fi
  rm -f "$f"
}
start() {
  rm -f "$PITV_RUN"/*.sock
  nohup "$ROOT/.venv/bin/pitv" web --port "$PORT" > "$PITV_DATA/web.out" 2>&1 & echo $! > "$PITV_RUN/web.pid"
  nohup "$ROOT/.venv/bin/pitv" play > "$PITV_DATA/player.out" 2>&1 & echo $! > "$PITV_RUN/player.pid"
  sleep 4
  status
}
status() {
  for n in web player; do
    f="$PITV_RUN/$n.pid"
    if [ -f "$f" ] && kill -0 "$(cat "$f")" 2>/dev/null; then echo "$n: running (pid $(cat "$f"))"; else echo "$n: stopped"; fi
  done
  echo "web UI: http://127.0.0.1:$PORT/"
}
case "${1:-status}" in
  start) start ;;
  stop) stop_one player; stop_one web; echo stopped ;;
  restart) stop_one player; stop_one web; start ;;
  status) status ;;
  *) echo "usage: $0 start|stop|restart|status"; exit 1 ;;
esac
