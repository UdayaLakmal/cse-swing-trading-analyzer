#!/usr/bin/env bash
# Install (or remove with --uninstall) the cse-daily systemd user timer.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

if [ "${1:-}" = "--uninstall" ]; then
  systemctl --user disable --now cse-daily.timer 2>/dev/null || true
  rm -f "$DEST/cse-daily.service" "$DEST/cse-daily.timer"
  systemctl --user daemon-reload
  echo "Removed the cse-daily timer."
  exit 0
fi

mkdir -p "$DEST"
for unit in cse-daily.service cse-daily.timer; do
  sed "s#@REPO@#$REPO#g" "$REPO/deploy/systemd/$unit" > "$DEST/$unit"
done
chmod +x "$REPO/scripts/cse-daily.sh"
systemctl --user daemon-reload
systemctl --user enable --now cse-daily.timer
echo "Installed. Next run:"
systemctl --user list-timers cse-daily.timer --no-pager
