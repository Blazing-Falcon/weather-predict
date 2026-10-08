#!/usr/bin/env bash
# Install the dev-machine systemd user timers for this checkout.
# usage: install-timers.sh [name ...]   (e.g. imerg bmkg; default: all)
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
uv=$(command -v uv)
dest="$HOME/.config/systemd/user"
mkdir -p "$dest"
names=("$@")
[ ${#names[@]} -eq 0 ] && names=($(ls "$repo"/ops/systemd/*.timer | sed -E 's|.*/nowcast-(.*)\.timer|\1|'))
for name in "${names[@]}"; do for unit in "$repo"/ops/systemd/nowcast-"$name".{service,timer}; do
  sed -e "s|@REPO@|$repo|g" -e "s|@UV@|$uv|g" "$unit" > "$dest/$(basename "$unit")"
done; done
systemctl --user daemon-reload
for name in "${names[@]}"; do
  systemctl --user enable --now "nowcast-$name.timer"
done
systemctl --user list-timers 'nowcast-*'
