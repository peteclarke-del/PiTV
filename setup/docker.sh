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
#   setup/docker.sh timer      install a user timer that runs `ensure` every five minutes,
#                              and one that runs `cookies` daily where cookies are configured
#   setup/docker.sh cookies    copy one site's cookies from the desktop's browser to pitv_content
#
# Docker restarts a container that stops, but a restart policy only covers a container that has
# started: one that could not start at boot (the library shares not answering yet, or the
# player's display not there until somebody logs in) is never tried again. The timer tries.
#
# pitv_content's container has no browser, and a browser's cookies are encrypted with a key in
# the desktop's keyring, which a container cannot reach. `cookies` runs on the host, where the
# keyring is, and writes the cookies of the one site named in docker/.env to a file in
# pitv_content's state folder; its "Cookies file" setting names that file. A site replaces its
# cookies as the browser is used, so the file is written again daily.
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

cookies() {
  local browser domain python out tmp kept
  browser="$(setting COOKIE_BROWSER)"; domain="$(setting COOKIE_DOMAIN)"; python="$(setting COOKIE_PYTHON)"
  [ -n "$browser" ] && [ -n "$domain" ] || { echo "COOKIE_BROWSER and COOKIE_DOMAIN are not set in $ENV_FILE" >&2; exit 1; }
  out="$ROOT/.dev/content-state/cookies.txt"
  umask 077
  tmp="$(mktemp -d)"
  # The export holds every site's cookies, so it is destroyed whatever happens next.
  # shellcheck disable=SC2064  # expanded now: $tmp is local and gone by the time the trap runs
  trap "find '$tmp' -type f -exec shred -u {} + 2>/dev/null; rm -rf '$tmp'" EXIT
  # yt-dlp writes the browser's cookies out when it is given a page to fetch, whether or not
  # the page exists; this one does not, so nothing is fetched.
  "${python:-python3}" -m yt_dlp --cookies-from-browser "$browser" --cookies "$tmp/all.txt" \
      --skip-download --quiet --no-warnings "https://example.invalid/" >/dev/null 2>&1 || true
  [ -s "$tmp/all.txt" ] || { echo "no cookies read from $browser: is it installed, and has ${python:-python3} the yt_dlp module?" >&2; exit 1; }
  { echo "# Netscape HTTP Cookie File"
    awk -F'\t' -v d="$domain" '!/^#/ && NF >= 7 && $7 != "" && ($1 == d || $1 == "." d || substr($1, length($1) - length(d)) == "." d)' "$tmp/all.txt"
  } > "$tmp/site.txt"
  kept="$(grep -vc '^#' "$tmp/site.txt" || true)"
  if [ "$kept" -eq 0 ]; then
    echo "no readable cookies for $domain in $browser. Either the browser is not signed in there, or the" >&2
    echo "keyring could not be read: ${python:-python3} needs the secretstorage module and a desktop session." >&2
    exit 1
  fi
  mkdir -p "$(dirname "$out")"
  mv "$tmp/site.txt" "$out"
  echo "$kept cookies for $domain written to $out"
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
  if [ -n "$(setting COOKIE_DOMAIN)" ]; then
    cat > "$dir/pitv-docker-cookies.service" <<UNIT
[Unit]
Description=Copy the browser's cookies for one site to pitv_content

[Service]
Type=oneshot
ExecStart="$ROOT/setup/docker.sh" cookies
UNIT
    cat > "$dir/pitv-docker-cookies.timer" <<UNIT
[Unit]
Description=Keep pitv_content's cookies current

[Timer]
OnStartupSec=3min
OnUnitActiveSec=1d

[Install]
WantedBy=timers.target
UNIT
  fi
  systemctl --user daemon-reload
  systemctl --user enable --now pitv-docker-ensure.timer
  [ -z "$(setting COOKIE_DOMAIN)" ] || systemctl --user enable --now pitv-docker-cookies.timer
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
  cookies) cookies ;;
  down) compose down ;;
  restart) compose restart ;;
  status) compose ps ;;
  logs) shift; compose logs -f --tail 50 "$@" ;;
  *) sed -n '2,21p' "$0"; exit 1 ;;
esac
