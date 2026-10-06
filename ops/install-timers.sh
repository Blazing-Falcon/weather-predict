#!/usr/bin/env bash
# Install the dev-machine systemd user timers for this checkout.
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
uv=$(command -v uv)
dest="$HOME/.config/systemd/user"
mkdir -p "$dest"
for unit in "$repo"/ops/systemd/*.{service,timer}; do
  sed -e "s|@REPO@|$repo|g" -e "s|@UV@|$uv|g" "$unit" > "$dest/$(basename "$unit")"
done
systemctl --user daemon-reload
for timer in "$repo"/ops/systemd/*.timer; do
  systemctl --user enable --now "$(basename "$timer")"
done
systemctl --user list-timers 'nowcast-*'
