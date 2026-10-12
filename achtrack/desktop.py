"""Starting with the desktop: an autostart entry on Linux, a LaunchAgent on macOS, a Run key on Windows."""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .config import APP_ICON, APP_ID, APP_NAME, AUTOSTART_FILE

IS_MAC = sys.platform == "darwin"
IS_WINDOWS = os.name == "nt"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
LAUNCH_AGENT = Path.home() / "Library" / "LaunchAgents" / f"com.{APP_ID}.agent.plist"


def windows_program() -> list:
    """How to start AchTrack on Windows, without a console window appearing."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    windowless = Path(sys.executable).with_name("pythonw.exe")
    return [str(windowless if windowless.is_file() else sys.executable), "-m", APP_ID]


def launch_command() -> str:
    if IS_WINDOWS:
        return subprocess.list2cmdline(windows_program())
    exe = shutil.which(APP_ID)
    return shlex.quote(exe) if exe else f"{shlex.quote(sys.executable)} -m {APP_ID}"


def _run_key(write: bool = False):
    import winreg
    return winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                          winreg.KEY_SET_VALUE if write else winreg.KEY_QUERY_VALUE)


def _set_run_key(enabled: bool) -> None:
    """Windows starts what is listed under the user's own Run key at sign-in."""
    import winreg
    try:
        with _run_key(write=True) as key:
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, f"{launch_command()} --background")
            else:
                winreg.DeleteValue(key, APP_NAME)
    except FileNotFoundError:
        pass            # already not there


def autostart_enabled() -> bool:
    if IS_WINDOWS:
        import winreg
        try:
            with _run_key() as key:
                winreg.QueryValueEx(key, APP_NAME)
            return True
        except OSError:
            return False
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
    if IS_WINDOWS:
        _set_run_key(enabled)
        return
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
