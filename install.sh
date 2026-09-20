#!/usr/bin/env bash
# Installs AchTrack for the current user on Linux or macOS.
#   ./install.sh              install / update
#   ./install.sh --autostart  also start it in the background at login
set -euo pipefail
cd "$(dirname "$0")"

OS="$(uname -s)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
VENV="$DATA/achtrack/venv"
BIN="$HOME/.local/bin"

if ! python3 -c "import venv, ensurepip" 2>/dev/null; then
    if [ "$OS" = "Darwin" ]; then
        echo "Python 3 with venv support is missing. Install it with:  brew install python"
    else
        echo "Python venv support is missing. On Pop!_OS / Ubuntu run:  sudo apt install python3-venv"
    fi
    exit 1
fi

if [ "$OS" = "Linux" ]; then
    # Qt 6 needs libxcb-cursor for its X11 backend (used so popups can sit on top of games).
    if ! ldconfig -p 2>/dev/null | grep libxcb-cursor >/dev/null; then
        echo "Missing libxcb-cursor0 (needed by Qt). On Pop!_OS / Ubuntu run:"
        echo "    sudo apt install libxcb-cursor0"
        exit 1
    fi
fi

# The screen watcher reads on-screen text with tesseract; everything else works without it.
if ! command -v tesseract >/dev/null 2>&1; then
    echo "Note: tesseract isn't installed, so the screen watcher will stay off. To enable it run:"
    [ "$OS" = "Darwin" ] && echo "    brew install tesseract" || echo "    sudo apt install tesseract-ocr"
    echo
fi

echo "Creating virtualenv in $VENV"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet --upgrade .

mkdir -p "$BIN"
ln -sf "$VENV/bin/achtrack" "$BIN/achtrack"

if [ "$OS" = "Darwin" ]; then
    APP="$HOME/Applications/AchTrack.app"
    BUILD="$DATA/achtrack/build"
    rm -rf "$BUILD"
    mkdir -p "$BUILD"
    "$VENV/bin/python" tools/make_icons.py "$BUILD" >/dev/null 2>&1 || true
    # A framework-Python app always reports itself as "Python" (its main bundle is the framework's
    # Python.app), so build a real bundle with its own launcher binary instead.
    echo "Building AchTrack.app (a minute or so)…"
    "$VENV/bin/pip" install --quiet --upgrade pyinstaller
    if "$VENV/bin/pyinstaller" --noconfirm --windowed --clean --log-level WARN \
            --name AchTrack --icon "$BUILD/AchTrack.icns" --osx-bundle-identifier com.achtrack.app \
            --add-data "$PWD/achtrack/assets:achtrack/assets" \
            --distpath "$BUILD/dist" --workpath "$BUILD/work" --specpath "$BUILD" \
            tools/mac_entry.py >/dev/null 2>&1 && [ -d "$BUILD/dist/AchTrack.app" ]; then
        rm -rf "$APP"
        mv "$BUILD/dist/AchTrack.app" "$APP"
        rm -rf "$BUILD/work" "$BUILD/dist"
        /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
            -f "$APP" >/dev/null 2>&1 || true
        echo "Added $APP (find it in Finder → Applications)."
    else
        echo "Couldn't build the app bundle; the 'achtrack' command still works."
    fi
else
    mkdir -p "$DATA/applications" "$DATA/icons/hicolor/scalable/apps"
    cp achtrack/assets/achtrack.svg "$DATA/icons/hicolor/scalable/apps/achtrack.svg"
    # Desktops rasterize SVGs with their own renderers, which don't all agree; ship PNGs too.
    ICONS="$DATA/achtrack/icons"
    rm -rf "$ICONS"
    if "$VENV/bin/python" tools/make_icons.py "$ICONS" --pngs >/dev/null 2>&1; then
        for size in 16 32 48 64 128 256 512; do
            mkdir -p "$DATA/icons/hicolor/${size}x${size}/apps"
            cp "$ICONS/achtrack-${size}.png" "$DATA/icons/hicolor/${size}x${size}/apps/achtrack.png"
        done
        command -v gtk-update-icon-cache >/dev/null 2>&1 && \
            gtk-update-icon-cache -f -t "$DATA/icons/hicolor" >/dev/null 2>&1 || true
    fi
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
fi

if [[ "${1:-}" == "--autostart" ]]; then
    "$VENV/bin/python" -c "from achtrack.desktop import set_autostart; set_autostart(True)"
    echo "Autostart enabled."
fi

echo
echo "Installed. Run:  achtrack"
case ":$PATH:" in
    *":$BIN:"*) ;;
    *) echo "Note: $BIN is not on your PATH - log out and back in, or add it to your shell profile." ;;
esac
