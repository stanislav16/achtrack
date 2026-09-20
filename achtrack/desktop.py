"""Freedesktop integration: autostart entry and opening folders/URLs."""
from __future__ import annotations

import shlex
import shutil
import sys
from pathlib import Path

from .config import APP_ICON, APP_ID, APP_NAME, AUTOSTART_FILE

IS_MAC = sys.platform == "darwin"
LAUNCH_AGENT = Path.home() / "Library" / "LaunchAgents" / f"com.{APP_ID}.agent.plist"


def launch_command() -> str:
    exe = shutil.which(APP_ID)
    return shlex.quote(exe) if exe else f"{shlex.quote(sys.executable)} -m {APP_ID}"


def autostart_enabled() -> bool:
    return LAUNCH_AGENT.is_file() if IS_MAC else AUTOSTART_FILE.is_file()


def _set_launch_agent(enabled: bool) -> None:
    """macOS starts background apps from a LaunchAgent plist."""
    if not enabled:
        LAUNCH_AGENT.unlink(missing_ok=True)
        return
    LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
    program = shutil.which(APP_ID) or f"{sys.executable} -m {APP_ID}"
    args = "".join(f"\n        <string>{part}</string>" for part in program.split() + ["--background"])
    LAUNCH_AGENT.write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.{APP_ID}.agent</string>
    <key>ProgramArguments</key>
    <array>{args}
    </array>
    <key>RunAtLoad</key><true/>
</dict>
</plist>
""", encoding="utf-8")


def set_autostart(enabled: bool) -> None:
    if IS_MAC:
        _set_launch_agent(enabled)
        return
    if not enabled:
        AUTOSTART_FILE.unlink(missing_ok=True)
        return
    AUTOSTART_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTOSTART_FILE.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        "Comment=Achievement and playtime tracker\n"
        f"Exec={launch_command()} --background\n"
        f"Icon={APP_ICON}\n"
        "X-GNOME-Autostart-enabled=true\n"
        "X-GNOME-Autostart-Delay=5\n",
        encoding="utf-8",
    )
