#!/usr/bin/env bash
# Desktop development helper: the web service, the windowed player and pitv_content's API,
# all against .dev and a RAM-backed cache, wired together as they are on the Pi.
#
#   setup/dev.sh setup     fake library, pitv_content sources, PiTV cache settings (once)
#   setup/dev.sh start | stop | restart | status
#   setup/dev.sh index     pitv_content re-indexes the fake NAS; PiTV imports the index
#   setup/dev.sh cache     pitv_content works PiTV's manifest into the cache (the admin's Run now)
#
# pitv_content is found on PATH or at PITV_CONTENT_BIN (default: a PiTV_content checkout two
# levels up with its own .venv). Without it, web and player still run.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PITV_DATA="$ROOT/.dev" PITV_RUN="$ROOT/.dev/run" PITV_WINDOWED=1
export PITV_MPV_ARGS="${PITV_MPV_ARGS:-}"   # the windowed player already mimics the Pi: 768x576, 4:3 PAL
export PITV_CONTENT_STATE="$PITV_DATA/content-state" PITV_CONTENT_WORK="$PITV_DATA/content-work"
PORT="${PORT:-8080}"
CONTENT_PORT="${CONTENT_PORT:-8091}"       # 8081 is the Pi's default; often taken on a desktop
CACHE_MOUNT="${PITV_CACHE_MOUNT:-/dev/shm/pitvcache}"   # pitv_content wants a mount point that is not /
CACHE="$CACHE_MOUNT/pitv"
LIB="$PITV_DATA/library"
WEB="http://127.0.0.1:$PORT/api"
CONTENT_BIN="${PITV_CONTENT_BIN:-$(command -v pitv-content || echo "$ROOT/../../PiTV_content/.venv/bin/pitv-content")}"
mkdir -p "$PITV_RUN"

running() { [ -f "$PITV_RUN/$1.pid" ] && kill -0 "$(cat "$PITV_RUN/$1.pid")" 2>/dev/null; }
stop_one() {  # name
  if running "$1"; then kill "$(cat "$PITV_RUN/$1.pid")"; sleep 1; fi
  rm -f "$PITV_RUN/$1.pid"
}
have_content() { [ -x "$CONTENT_BIN" ]; }
wait_for() {  # url
  for _ in $(seq 1 30); do curl -sf "$1" >/dev/null && return 0; sleep 1; done
  echo "no answer from $1" >&2; return 1
}

setup() {
  mkdir -p "$CACHE/index" "$CACHE/logs" "$CACHE/reports" "$CACHE/acquired" "$PITV_CONTENT_STATE" "$PITV_CONTENT_WORK"
  [ -f "$LIB/library.json" ] || "$ROOT/.venv/bin/pitv" fake-library "$LIB"
  "$ROOT/.venv/bin/python" - "$CACHE" "$CONTENT_PORT" <<'PY'
import sys
from pitv import db
from pitv.config import load_config
cache, port = sys.argv[1], sys.argv[2]
conn = db.connect(load_config().db_path)
for key, value in {"cache_dir": cache, "acquire_dir": f"{cache}/acquired",
                   "content_tool_url": f"http://127.0.0.1:{port}"}.items():
    db.set_setting(conn, key, value)
print(f"PiTV: cache {cache}, pitv_content at http://127.0.0.1:{port}")
PY
  if have_content; then
    seed=$("$ROOT/.venv/bin/python" -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1]))["sources"]))' "$LIB/library.json")
    "$CONTENT_BIN" sources --seed "$seed" --acquire-dir "$CACHE/acquired"
  else
    echo "pitv_content not found at $CONTENT_BIN; set PITV_CONTENT_BIN" >&2
  fi
}
start() {
  rm -f "$PITV_RUN"/*.sock
  mkdir -p "$CACHE"
  nohup "$ROOT/.venv/bin/pitv" web --port "$PORT" > "$PITV_DATA/web.out" 2>&1 & echo $! > "$PITV_RUN/web.pid"
  nohup "$ROOT/.venv/bin/pitv" play > "$PITV_DATA/player.out" 2>&1 & echo $! > "$PITV_RUN/player.pid"
  if have_content; then
    nohup "$CONTENT_BIN" serve --host 127.0.0.1 --port "$CONTENT_PORT" --cache-dir "$CACHE" \
      --manifest "$WEB/content/manifest?days=1" --report-url "$WEB/content/report" \
      > "$PITV_DATA/content-serve.out" 2>&1 & echo $! > "$PITV_RUN/content.pid"
  fi
  wait_for "http://127.0.0.1:$PORT/"
  status
}
status() {
  for n in web player content; do
    if running "$n"; then echo "$n: running (pid $(cat "$PITV_RUN/$n.pid"))"; else echo "$n: stopped"; fi
  done
  echo "web UI: http://127.0.0.1:$PORT/   pitv_content API: http://127.0.0.1:$CONTENT_PORT/api/status   cache: $CACHE"
}
post() {  # path json
  curl -sf -X POST -H 'content-type: application/json' -d "$2" "$WEB/$1"; echo
}
case "${1:-status}" in
  setup) setup ;;
  start) start ;;
  stop) stop_one content; stop_one player; stop_one web; echo stopped ;;
  restart) stop_one content; stop_one player; stop_one web; start ;;
  status) status ;;
  index) post catalogue/refresh '{"reindex": true}' ;;
  cache) post content/tool/run '{}' ;;
  *) echo "usage: $0 setup|start|stop|restart|status|index|cache"; exit 1 ;;
esac
