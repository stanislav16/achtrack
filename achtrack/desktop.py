"""Freedesktop integration: autostart entry and opening folders/URLs."""
from __future__ import annotations

import shlex
import shutil
import sys

from .config import APP_ICON, APP_ID, APP_NAME, AUTOSTART_FILE


def launch_command() -> str:
    exe = shutil.which(APP_ID)
    return shlex.quote(exe) if exe else f"{shlex.quote(sys.executable)} -m {APP_ID}"


def autostart_enabled() -> bool:
    return AUTOSTART_FILE.is_file()


def set_autostart(enabled: bool) -> None:
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
