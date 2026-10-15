#!/usr/bin/env bash
# Install the pinned Go toolchain on an Ubuntu runner, for the kit's gate
# (make kit-verify). Used by ci.yml's kit job.
#
# Why not an action: this follows tools/install-pandoc.sh, which is already
# reviewed here, and adds no third-party action. The archive is fetched from
# go.dev over https with bounded retries and refused unless its sha256 matches.
# The digest is the one go.dev publishes for this file (https://go.dev/dl/,
# read 2026-10-15), so the pin does not rest on a first download.
#
# kit/go.mod pins `toolchain go1.27.0`, and the gate runs with GOTOOLCHAIN=local,
# so a different Go here fails the build instead of downloading another one.
#
# Prints the directory to put on PATH, and appends it to $GITHUB_PATH when set.
set -euo pipefail

VERSION="1.27.0"
SHA256="675c26c449cbb18fc24b74650de1eabbae6e16f64326fd85a283fb3b58280685"
URL="https://go.dev/dl/go${VERSION}.linux-amd64.tar.gz"

if [ "$(uname -s)-$(uname -m)" != "Linux-x86_64" ]; then
  echo "install-go.sh pins the linux-amd64 archive; this machine is $(uname -s)-$(uname -m)" >&2
  exit 1
fi

dest="${RUNNER_TEMP:-$(mktemp -d)}/go-${VERSION}"
archive="$(mktemp --suffix=.tar.gz)"
trap 'rm -f "$archive"' EXIT
curl --fail --silent --show-error --location \
  --proto '=https' --proto-redir '=https' --tlsv1.2 \
  --retry 5 --retry-delay 5 --retry-all-errors --max-time 300 \
  --output "$archive" "$URL"
echo "${SHA256}  ${archive}" | sha256sum --check --strict -
mkdir -p "$dest"
tar -C "$dest" -xzf "$archive"
if [ -n "${GITHUB_PATH:-}" ]; then
  echo "${dest}/go/bin" >> "$GITHUB_PATH"
fi
"${dest}/go/bin/go" version
echo "${dest}/go/bin"
