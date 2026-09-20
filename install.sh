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
    rm -rf "$APP"
    mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
    # macOS names an app after the binary it runs, and only treats it as this bundle when that
    # binary IS Contents/MacOS/<CFBundleExecutable>. So the interpreter itself is copied in under
    # the app's name; pyvenv.cfg + lib keep it pointed at the virtualenv, and the Info.plist sets
    # ACHTRACK_APP=1, which sitecustomize.py (below) turns into "start the app".
    REAL_PYTHON="$("$VENV/bin/python" -c 'import os, sys; print(os.path.realpath(sys.executable))')"
    cp "$REAL_PYTHON" "$APP/Contents/MacOS/AchTrack"
    cp "$VENV/pyvenv.cfg" "$APP/Contents/pyvenv.cfg"
    ln -sfn "$VENV/lib" "$APP/Contents/lib"
    SITE="$("$VENV/bin/python" -c 'import site; print(site.getsitepackages()[0])')"
    cat > "$SITE/sitecustomize.py" <<'BOOT'
"""Launched from AchTrack.app, the interpreter starts the app instead of a REPL."""
import os
import sys

if os.environ.pop("ACHTRACK_APP", None) == "1" and not sys.argv[1:]:
    from achtrack.__main__ import main
    # os._exit, because raising SystemExit while the site module is still loading is fatal.
    os._exit(main([]) or 0)
BOOT
    "$VENV/bin/python" tools/make_icons.py "$APP/Contents/Resources" >/dev/null 2>&1 || true
    cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>AchTrack</string>
    <key>CFBundleDisplayName</key><string>AchTrack</string>
    <key>CFBundleIdentifier</key><string>com.achtrack.app</string>
    <key>CFBundleExecutable</key><string>AchTrack</string>
    <key>CFBundleIconFile</key><string>AchTrack</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>1.0.0</string>
    <key>LSMinimumSystemVersion</key><string>11.0</string>
    <key>NSHighResolutionCapable</key><true/>
    <key>LSEnvironment</key>
    <dict><key>ACHTRACK_APP</key><string>1</string></dict>
</dict>
</plist>
EOF
    touch "$APP"          # nudge Finder to pick up the new icon
    echo "Added $APP (find it in Finder → Applications)."
else
    mkdir -p "$DATA/applications" "$DATA/icons/hicolor/scalable/apps"
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
