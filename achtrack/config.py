from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

APP_ID = "achtrack"
APP_NAME = "AchTrack"


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var)
    return Path(value) if value else Path.home() / fallback


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config") / APP_ID
DATA_DIR = _xdg("XDG_DATA_HOME", ".local/share") / APP_ID
CACHE_DIR = _xdg("XDG_CACHE_HOME", ".cache") / APP_ID
ICON_DIR = CACHE_DIR / "icons"
CONFIG_FILE = CONFIG_DIR / "config.json"
DB_FILE = DATA_DIR / "achtrack.db"
AUTOSTART_FILE = _xdg("XDG_CONFIG_HOME", ".config") / "autostart" / f"{APP_ID}.desktop"
ASSET_DIR = Path(__file__).resolve().parent / "assets"
APP_ICON = ASSET_DIR / "achtrack.svg"

POPUP_STYLES = ("steam", "system", "both", "none")
POPUP_CORNERS = ("bottom-right", "bottom-left", "top-right", "top-left")


@dataclass
class Config:
    # Free key from https://steamcommunity.com/dev/apikey - needed for achievement names/icons.
    steam_api_key: str = ""
    # SteamID64 or profile URL; empty = detect the account logged into the local Steam client.
    steam_id: str = ""
    # Copy owned Steam games, their playtime and achievements into the local library.
    steam_sync: bool = True
    steam_sync_interval: float = 900.0
    # PlayStation: NPSSO token from a signed-in browser (see Settings); trophies and play time.
    psn_npsso: str = ""
    psn_sync: bool = True
    psn_sync_interval: float = 1800.0
    # After a session of a game whose crack doesn't record achievements, ask what was earned.
    session_review: bool = True
    # Read games' save files (Unreal, Unity, JSON, XML...) to detect achievements the crack doesn't record.
    save_detection: bool = True
    # Read on-screen text (locally, with tesseract) while a game runs, as a fallback for unreadable saves.
    screen_watcher: bool = True
    screen_capture_interval: float = 0.5   # capture as often as this (slower if capturing itself takes longer)
    screen_monitor: int = 1
    language: str = "english"
    # Extra folders to search for Wine prefixes (Lutris, Bottles, custom locations...).
    scan_dirs: list = field(default_factory=list)
    popup_style: str = "steam"
    popup_corner: str = "bottom-right"
    popup_seconds: float = 6.0
    sound: bool = True
    sound_file: str = ""
    rare_sound_file: str = ""
    session_toast: bool = True
    # Qt on native Wayland can't place or raise popups, so run through XWayland by default.
    theme: str = "auto"                    # auto | dark | light
    font_family: str = ""                  # empty = the desktop's own UI font
    sort: str = "recent"                   # recent | name | playtime | progress | genre | platform
    platform_filter: str = "all"           # all | pc | psn
    force_x11: bool = True
    poll_interval: float = 1.5
    process_interval: float = 5.0
    rescan_interval: float = 45.0

    @classmethod
    def load(cls) -> "Config":
        try:
            raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        defaults = cls()
        values = {}
        for f in fields(cls):
            if f.name in raw and isinstance(raw[f.name], type(getattr(defaults, f.name))):
                values[f.name] = raw[f.name]
            elif f.name in raw and isinstance(getattr(defaults, f.name), float) and isinstance(raw[f.name], int):
                values[f.name] = float(raw[f.name])
        cfg = cls(**values)
        if cfg.popup_style not in POPUP_STYLES:
            cfg.popup_style = "steam"
        if cfg.popup_corner not in POPUP_CORNERS:
            cfg.popup_corner = "bottom-right"
        return cfg

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        tmp.replace(CONFIG_FILE)
