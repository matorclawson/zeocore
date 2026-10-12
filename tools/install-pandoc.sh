#!/usr/bin/env bash
# Install the pinned Pandoc on an Ubuntu runner, for the real authoring
# receipt tests. Used by ci.yml and by publish.yml's release gate.
#
# Why not `apt-get install pandoc`: on 2026-10-12, CI for zeocore #116 timed
# out because the Ubuntu mirror served Pandoc's 26.9 MB package at 54 kB/s
# (8 min 16 s). This fetches upstream's .deb of the same version from
# GitHub's CDN, with bounded retries and a timeout, and refuses it unless
# its sha256 matches. GitHub publishes no digest for the asset, so the pin
# was taken from the first download (2026-10-12) and is the trust root from
# then on.
set -euo pipefail

VERSION="3.1.3"
SHA256="caa7e0410f9e2cb1da2eb8db13cc97b5548fe455985e2c944e3929d22f99bcdc"
URL="https://github.com/jgm/pandoc/releases/download/${VERSION}/pandoc-${VERSION}-1-amd64.deb"

if [ "$(dpkg --print-architecture)" != "amd64" ]; then
  echo "install-pandoc.sh pins the amd64 .deb; this runner is $(dpkg --print-architecture)" >&2
  exit 1
fi

deb="$(mktemp --suffix=.deb)"
trap 'rm -f "$deb"' EXIT
curl --fail --silent --show-error --location \
  --proto '=https' --proto-redir '=https' --tlsv1.2 \
  --retry 5 --retry-delay 5 --retry-all-errors --max-time 120 \
  --output "$deb" "$URL"
echo "${SHA256}  ${deb}" | sha256sum --check --strict -
sudo dpkg -i "$deb"
pandoc --version | head -n 1
