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

# Which family of Linux this is, so the "install this" hints name real packages.
distro_family() {
    local id="" like="" release="${OS_RELEASE:-/etc/os-release}"
    if [ -r "$release" ]; then
        id="$(. "$release" 2>/dev/null && echo "${ID:-}")"
        like="$(. "$release" 2>/dev/null && echo "${ID_LIKE:-}")"
    fi
    case " $id $like " in
        *" debian "*|*" ubuntu "*)          echo debian ;;
        *" fedora "*|*" rhel "*|*" centos "*) echo fedora ;;
        *" arch "*|*" archlinux "*)         echo arch ;;
        *" suse "*|*" opensuse "*)          echo suse ;;
        *" alpine "*)                       echo alpine ;;
        *)
            # No usable os-release: go by whichever package manager is installed.
            for pm in apt-get dnf pacman zypper apk; do
                command -v "$pm" >/dev/null 2>&1 || continue
                case "$pm" in
                    apt-get) echo debian ;; dnf) echo fedora ;; pacman) echo arch ;;
                    zypper)  echo suse ;;   apk) echo alpine ;;
                esac
                return
            done
            echo unknown ;;
    esac
}

install_cmd() {
    case "$1" in
        debian) echo "sudo apt install" ;;
        fedora) echo "sudo dnf install" ;;
        arch)   echo "sudo pacman -S" ;;
        suse)   echo "sudo zypper install" ;;
        alpine) echo "sudo apk add" ;;
        *)      echo "" ;;
    esac
}

# What each thing AchTrack needs is called on each family.
package_for() {
    [ "$2" = "unknown" ] && { echo ""; return; }
    case "$1:$2" in
        venv:debian) echo python3-venv ;;
        venv:fedora|venv:suse|venv:alpine) echo python3 ;;
        venv:arch) echo python ;;
        libxcb-cursor.so.0:debian|libxcb-cursor.so.0:suse) echo libxcb-cursor0 ;;
        libxcb-cursor.so.0:*) echo xcb-util-cursor ;;
        libxkbcommon-x11.so.0:debian) echo libxkbcommon-x11-0 ;;
        libxkbcommon-x11.so.0:suse) echo libxkbcommon-x11-0 ;;
        libxkbcommon-x11.so.0:*) echo libxkbcommon-x11 ;;
        libGL.so.1:debian) echo libgl1 ;;
        libGL.so.1:fedora) echo mesa-libGL ;;
        libGL.so.1:suse) echo Mesa-libGL1 ;;
        libGL.so.1:arch) echo libglvnd ;;
        libGL.so.1:alpine) echo mesa-gl ;;
        libEGL.so.1:debian) echo libegl1 ;;
        libEGL.so.1:fedora) echo mesa-libEGL ;;
        libEGL.so.1:*) echo libglvnd ;;
        libxcb-xinerama.so.0:debian) echo libxcb-xinerama0 ;;
        libxcb-xinerama.so.0:*) echo xcb-util ;;
        libxcb-icccm.so.4:debian) echo libxcb-icccm4 ;;
        libxcb-icccm.so.4:*) echo xcb-util-wm ;;
        libxcb-image.so.0:debian) echo libxcb-image0 ;;
        libxcb-image.so.0:*) echo xcb-util-image ;;
        libxcb-keysyms.so.1:debian) echo libxcb-keysyms1 ;;
        libxcb-keysyms.so.1:*) echo xcb-util-keysyms ;;
        libxcb-render-util.so.0:debian) echo libxcb-render-util0 ;;
        libxcb-render-util.so.0:*) echo xcb-util-renderutil ;;
        libdbus-1.so.3:debian) echo libdbus-1-3 ;;
        libdbus-1.so.3:fedora|libdbus-1.so.3:suse) echo dbus-libs ;;
        libdbus-1.so.3:*) echo dbus ;;
        libfontconfig.so.1:debian) echo libfontconfig1 ;;
        libfontconfig.so.1:*) echo fontconfig ;;
        *) echo "" ;;
    esac
}

FAMILY="unknown"
[ "$OS" = "Linux" ] && FAMILY="$(distro_family)"

if ! python3 -c "import venv, ensurepip" 2>/dev/null; then
    echo "Python 3 with venv support is missing."
    if [ "$OS" = "Darwin" ]; then
        echo "Install it with:  brew install python"
    elif [ -n "$(install_cmd "$FAMILY")" ]; then
        echo "Install it with:  $(install_cmd "$FAMILY") $(package_for venv "$FAMILY")"
    else
        echo "Install your distribution's Python 3 package, including venv and ensurepip."
    fi
    exit 1
fi

echo "Creating virtualenv in $VENV"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
PIPLOG="$DATA/achtrack/install.log"
if ! "$VENV/bin/pip" install --quiet --upgrade . >"$PIPLOG" 2>&1; then
    tail -20 "$PIPLOG"
    echo
    echo "Installing the Python packages failed (full output in $PIPLOG)."
    echo "If pip couldn't find a PySide6 wheel, your"
    echo "system is one Qt doesn't publish one for (musl, 32-bit, older ARM). Install PySide6"
    echo "from your distribution instead, then run this script again."
    exit 1
fi

# Rather than guess which Qt packages a distribution splits out, ask Qt's X11 plugin what it is
# actually missing - then name the packages that carry those libraries.
check_qt_libs() {
    local plugin missing pkg pkgs="" unknown=""
    plugin="$(ls "$VENV"/lib/python*/site-packages/PySide6/Qt/plugins/platforms/libqxcb.so 2>/dev/null | head -1)"
    [ -r "$plugin" ] || return 0
    missing="$(ldd "$plugin" 2>/dev/null | awk '/not found/ {print $1}' | sort -u)"
    [ -n "$missing" ] || return 0
    for lib in $missing; do
        pkg="$(package_for "$lib" "$FAMILY")"
        if [ -n "$pkg" ]; then
            case " $pkgs " in *" $pkg "*) ;; *) pkgs="$pkgs $pkg" ;; esac
        else
            unknown="$unknown $lib"
        fi
    done
    echo
    echo "Qt is missing some system libraries, so the window won't open yet:"
    [ -n "$pkgs" ] && [ -n "$(install_cmd "$FAMILY")" ] && echo "    $(install_cmd "$FAMILY")$pkgs"
    [ -n "$pkgs" ] && [ -z "$(install_cmd "$FAMILY")" ] && echo "    install:$pkgs"
    [ -n "$unknown" ] && echo "    also needed, from whichever package provides them:$unknown"
}

mkdir -p "$BIN"
ln -sf "$VENV/bin/achtrack" "$BIN/achtrack"

# macOS 26 and later draw app icons themselves from a layered ".icon" document - the rounded
# square, the material and the shading are the system's, so the icon matches everything else on
# the dock. Compiling one needs actool, which comes with Xcode. Without it the .icns still works.
find_actool() {
    local found
    found="$(xcrun --find actool 2>/dev/null)" && [ -x "$found" ] && { echo "$found"; return; }
    for dir in "$(xcode-select -p 2>/dev/null)" /Applications/Xcode.app/Contents/Developer; do
        [ -x "$dir/usr/bin/actool" ] && { echo "$dir/usr/bin/actool"; return; }
    done
    echo ""
}

layered_icon() {
    local app="$1" build="$2" actool document plist
    [ "$(sw_vers -productVersion | cut -d. -f1)" -ge 26 ] 2>/dev/null || return 0
    actool="$(find_actool)"
    [ -n "$actool" ] || { echo "  (install Xcode for the layered macOS icon; using the flat one)"; return 0; }
    document="$build/AchTrack.icon"
    [ -d "$document" ] || return 0
    plist="$build/icon.plist"
    mkdir -p "$build/car"
    if ! "$actool" "$document" --compile "$build/car" --app-icon AchTrack \
            --output-partial-info-plist "$plist" --platform macosx \
            --minimum-deployment-target 26.0 --output-format human-readable-text \
            >"$build/actool.log" 2>&1 || [ ! -f "$build/car/Assets.car" ]; then
        echo "  (couldn't compile the layered icon - see $build/actool.log)"
        return 0
    fi
    cp "$build/car/Assets.car" "$app/Contents/Resources/Assets.car"
    # actool also renders a flat .icns from the same document, with the system's own tile and
    # shading - better than ours, and what gets used wherever the layered icon isn't.
    [ -f "$build/car/AchTrack.icns" ] && cp "$build/car/AchTrack.icns" "$app/Contents/Resources/AchTrack.icns"
    /usr/libexec/PlistBuddy -c "Set :CFBundleIconName AchTrack" "$app/Contents/Info.plist" >/dev/null 2>&1 || \
        /usr/libexec/PlistBuddy -c "Add :CFBundleIconName string AchTrack" "$app/Contents/Info.plist" >/dev/null 2>&1 || true
    echo "  Layered icon compiled."
}

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
        layered_icon "$APP" "$BUILD"
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
    command -v update-desktop-database >/dev/null 2>&1 && \
        update-desktop-database "$DATA/applications" >/dev/null 2>&1 || true
fi

if [[ "${1:-}" == "--autostart" ]]; then
    "$VENV/bin/python" -c "from achtrack.desktop import set_autostart; set_autostart(True)"
    echo "Autostart enabled."
fi

if [ "$OS" = "Linux" ]; then
    check_qt_libs
fi

echo
echo "Installed. Run:  achtrack"
case ":$PATH:" in
    *":$BIN:"*) ;;
    *) echo "Note: $BIN is not on your PATH - log out and back in, or add it to your shell profile." ;;
esac
