#!/usr/bin/env bash
# The development stack under Docker (docker/docker-compose.yml): PiTV's web service and player
# and pitv_content's API, restarted when they stop and started with Docker at boot.
#
#   setup/docker.sh build      build the image from both projects' dependencies
#   setup/docker.sh up         start the stack afresh, onto the current image and settings
#   setup/docker.sh down       stop it
#   setup/docker.sh restart    restart the services onto the code on disk
#   setup/docker.sh status     what is running, and its health
#   setup/docker.sh logs [svc] follow the logs
#   setup/docker.sh ensure     start whatever is not running; what the timer below runs
#   setup/docker.sh timer      install a user timer that runs `ensure` every five minutes
#
# Docker restarts a container that stops, but a restart policy only covers a container that has
# started: one that could not start at boot (the library shares not answering yet, or the
# player's display not there until somebody logs in) is never tried again. The timer tries.
#
# Everything particular to the machine is in docker/.env (from docker/.env.example).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DOCKER="$ROOT/docker"
ENV_FILE="$DOCKER/.env"
[ -f "$ENV_FILE" ] || { echo "no $ENV_FILE: copy docker/.env.example and fill it in" >&2; exit 1; }
compose() { docker-compose --project-name pitv --project-directory "$DOCKER" --env-file "$ENV_FILE" \
                           -f "$DOCKER/docker-compose.yml" "$@"; }
setting() { sed -n "s/^$1=//p" "$ENV_FILE" | tail -1; }

build() {
  # Only the dependencies go into the image; the source is mounted. Both pyproject.toml files are
  # staged beside the Dockerfile, which is all a build may read.
  rm -rf "$DOCKER/stage" && mkdir -p "$DOCKER/stage"
  cp "$ROOT/pyproject.toml" "$DOCKER/stage/pyproject-pitv.toml"
  cp "$(setting CONTENT_SRC)/pyproject.toml" "$DOCKER/stage/pyproject-content.toml"
  cp "$DOCKER/requirements.py" "$DOCKER/stage/"
  docker build -t pitv-dev:latest "$DOCKER"
}

stop_units() {
  # The stack as it ran before, as systemd-run units in the desktop session.
  for unit in pitv-dev-content pitv-dev-player pitv-dev-web; do
    if systemctl --user is-active --quiet "$unit" 2>/dev/null; then
      systemctl --user stop "$unit" && echo "stopped $unit"
    fi
  done
}

install_timer() {
  local dir="$HOME/.config/systemd/user"
  mkdir -p "$dir"
  cat > "$dir/pitv-docker-ensure.service" <<UNIT
[Unit]
Description=Start whatever of the PiTV development stack is not running

[Service]
Type=oneshot
ExecStart="$ROOT/setup/docker.sh" ensure
UNIT
  cat > "$dir/pitv-docker-ensure.timer" <<UNIT
[Unit]
Description=Keep the PiTV development stack up

[Timer]
OnStartupSec=1min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
UNIT
  systemctl --user daemon-reload
  systemctl --user enable --now pitv-docker-ensure.timer
}

case "${1:-}" in
  build) build ;;
  up) docker image inspect pitv-dev:latest >/dev/null 2>&1 || build
      stop_units
      # Removed and created afresh, never recreated in place: docker-compose 1.29 cannot read a
      # current Docker engine's image config when it recreates (KeyError 'ContainerConfig'), and
      # on 27 September that left the web service renamed and stopped.
      compose down --remove-orphans
      compose up -d ;;
  ensure) compose up -d --no-recreate ;;
  timer) install_timer ;;
  down) compose down ;;
  restart) compose restart ;;
  status) compose ps ;;
  logs) shift; compose logs -f --tail 50 "$@" ;;
  *) sed -n '2,19p' "$0"; exit 1 ;;
esac
