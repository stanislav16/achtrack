#!/usr/bin/env bash
# Removes the app. Your library (playtime + unlocks) in ~/.local/share/achtrack/achtrack.db is kept
# unless you pass --purge.
set -euo pipefail
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}"

"$HOME/.local/bin/achtrack" quit 2>/dev/null || true
rm -rf "$DATA/achtrack/venv"
rm -f "$HOME/.local/bin/achtrack" "$DATA/applications/achtrack.desktop" \
      "$DATA/icons/hicolor/scalable/apps/achtrack.svg" "$CONFIG/autostart/achtrack.desktop"

if [[ "${1:-}" == "--purge" ]]; then
    rm -rf "$DATA/achtrack" "$CONFIG/achtrack" "${XDG_CACHE_HOME:-$HOME/.cache}/achtrack"
    echo "Removed AchTrack and all its data."
else
    echo "Removed AchTrack. Your library is still in $DATA/achtrack (use --purge to delete it)."
fi
