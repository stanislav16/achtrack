#!/usr/bin/env bash
# Installs AchTrack for the current user: venv, `achtrack` command, menu entry, optional autostart.
#   ./install.sh              install / update
#   ./install.sh --autostart  also start it in the background at login
set -euo pipefail
cd "$(dirname "$0")"

DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
VENV="$DATA/achtrack/venv"
BIN="$HOME/.local/bin"

if ! python3 -c "import venv, ensurepip" 2>/dev/null; then
    echo "Python venv support is missing. On Pop!_OS / Ubuntu run:"
    echo "    sudo apt install python3-venv"
    exit 1
fi

# Qt 6 needs libxcb-cursor for its X11 backend (used so popups can sit on top of games).
if ! ldconfig -p 2>/dev/null | grep libxcb-cursor >/dev/null; then
    echo "Missing libxcb-cursor0 (needed by Qt). On Pop!_OS / Ubuntu run:"
    echo "    sudo apt install libxcb-cursor0"
    exit 1
fi

echo "Creating virtualenv in $VENV"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet --upgrade .

mkdir -p "$BIN" "$DATA/applications" "$DATA/icons/hicolor/scalable/apps"
ln -sf "$VENV/bin/achtrack" "$BIN/achtrack"
cp achtrack/assets/achtrack.svg "$DATA/icons/hicolor/scalable/apps/achtrack.svg"

cat > "$DATA/applications/achtrack.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=AchTrack
GenericName=Achievement Tracker
Comment=Steam achievements, unlock popups and playtime for your games
Exec=$BIN/achtrack
Icon=achtrack
Categories=Game;Utility;
StartupWMClass=achtrack
EOF

if [[ "${1:-}" == "--autostart" ]]; then
    "$BIN/achtrack" --version >/dev/null
    "$VENV/bin/python" -c "from achtrack.desktop import set_autostart; set_autostart(True)"
    echo "Autostart enabled."
fi

echo
echo "Installed. Start it from the app menu or run:  achtrack"
case ":$PATH:" in
    *":$BIN:"*) ;;
    *) echo "Note: $BIN is not on your PATH - log out and back in, or add it to ~/.bashrc." ;;
esac
