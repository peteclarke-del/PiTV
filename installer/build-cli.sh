#!/usr/bin/env bash
# Cross-compile the installer for Linux and Windows into dist/.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$(cd "$HERE/.." && pwd)/dist"
mkdir -p "$OUT"
cd "$HERE/cli"
VERSION="$(git describe --tags --always --dirty 2>/dev/null || echo dev)"
for target in linux/amd64 linux/arm64 windows/amd64; do
  os="${target%/*}" arch="${target#*/}"
  bin="$OUT/pitv-installer-$os-$arch"
  [ "$os" = windows ] && bin="$bin.exe"
  GOOS="$os" GOARCH="$arch" CGO_ENABLED=0 go build -trimpath -ldflags "-s -w -X main.version=$VERSION" -o "$bin" .
  echo "built $bin"
done
echo "version $VERSION"
