# The development stack in containers: PiTV's web service and player, and pitv_content's API,
# on the host's own Ubuntu so mpv, ffmpeg and Python match what the code is tested against.
# The source is not copied in: both repositories are mounted at their host paths (see
# docker-compose.yml), so a restart runs the code as it is on disk. Only the dependencies are
# installed here, read from both projects' pyproject.toml, which setup/docker.sh stages beside
# this file.
FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv ffmpeg mpv \
        intel-media-va-driver mesa-va-drivers libgl1-mesa-dri \
        fonts-dejavu-core fonts-dejavu-extra fonts-liberation \
        ca-certificates curl smbclient tini tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY stage/ /build/
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/python /build/requirements.py /build/pyproject-*.toml > /build/requirements.txt \
    && /opt/venv/bin/pip install --no-cache-dir -r /build/requirements.txt

COPY bin/ /usr/local/bin/
# The services run as the host user (compose sets it), whose files these are; its home holds
# yt-dlp's cache, and its runtime directory takes the display and sound sockets.
RUN chmod 755 /usr/local/bin/* \
    && mkdir -p /home/pitv /run/user/1000 && chown 1000:1000 /home/pitv /run/user/1000 && chmod 700 /run/user/1000

ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 HOME=/home/pitv
ENTRYPOINT ["tini", "--"]
