#!/usr/bin/env bash
# Cross-compile the installer for Linux and Windows into dist/.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/../dist"; mkdir -p "$OUT"
cd "$HERE/cli"
VERSION="$(git describe --tags --always 2>/dev/null || echo dev)"
GOOS=linux   GOARCH=amd64 go build -trimpath -ldflags "-s -w" -o "$OUT/pitv-installer-linux-amd64" .
GOOS=linux   GOARCH=arm64 go build -trimpath -ldflags "-s -w" -o "$OUT/pitv-installer-linux-arm64" .
GOOS=windows GOARCH=amd64 go build -trimpath -ldflags "-s -w" -o "$OUT/pitv-installer-windows-amd64.exe" .
echo "built $VERSION into $OUT"; ls -la "$OUT" | grep pitv-installer
