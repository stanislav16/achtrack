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


if os.name == "nt":
    # Settings roam with the account; the database and the artwork cache stay on this machine.
    _local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / APP_NAME
    CONFIG_DIR = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / APP_NAME
    DATA_DIR = _local
    CACHE_DIR = _local / "cache"
else:
    CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config") / APP_ID
    DATA_DIR = _xdg("XDG_DATA_HOME", ".local/share") / APP_ID
    CACHE_DIR = _xdg("XDG_CACHE_HOME", ".cache") / APP_ID
ICON_DIR = CACHE_DIR / "icons"
CONFIG_FILE = CONFIG_DIR / "config.json"
DB_FILE = DATA_DIR / "achtrack.db"
BACKUP_DIR = DATA_DIR / "backups"
EXPORT_DIR = DATA_DIR / "phone"
SHARE_DIR = DATA_DIR / "share"
AUTOSTART_FILE = _xdg("XDG_CONFIG_HOME", ".config") / "autostart" / f"{APP_ID}.desktop"
ASSET_DIR = Path(__file__).resolve().parent / "assets"
APP_ICON = ASSET_DIR / "achtrack.svg"
# Windows draws taskbar and title-bar icons from an .ico, with each size drawn for that size.
WINDOW_ICON = ASSET_DIR / "achtrack.ico" if os.name == "nt" else APP_ICON
TRAY_ICON = ASSET_DIR / "achtrack-symbolic.svg"   # monochrome, for system trays

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
    # Steam never reveals a hidden achievement's description; PlayStation always does. Look the
    # blanks up in the game's trophy list (needs the PlayStation login above).
    psn_describe_hidden: bool = True
    # After a session of a game whose crack doesn't record achievements, ask what was earned.
    session_review: bool = True
    # Read games' save files (Unreal, Unity, JSON, XML...) to detect achievements the crack doesn't record.
    save_detection: bool = True
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
    show_icons: bool = True                # game artwork thumbnails in the library list
    achievement_sort: str = "default"      # default | rarest | common | recent | name
    # Backups: dated database snapshots + a readable JSON export.
    backup_dir: str = ""                   # empty = ~/.local/share/achtrack/backups
    backup_keep: int = 10
    backup_daily: bool = True
    # Phone page: one self-contained file, encrypted with this passphrase, optionally on Vercel.
    export_passphrase: str = ""
    export_dir: str = ""                   # empty = ~/.local/share/achtrack/phone
    export_auto: bool = True               # rebuild after each session and with the daily backup
    # Pack a game's save files into the shared folder when you stop playing, so the other machine
    # can pick up where you left off. Restoring is always something you ask for.
    welcomed: bool = False                 # the first-run welcome has been shown
    saves_auto_backup: bool = True
    saves_offer_on_play: bool = True       # Play offers a newer save from your other machine
    vercel_token: str = ""
    vercel_project: str = "achtrack"
    # The page you send friends: the name shown on it, and its own Vercel project so it never
    # lands on top of the private phone page.
    share_name: str = ""
    share_project: str = "achtrack-stats"
    vercel_team: str = ""                  # team id (team_...) or slug; empty = personal account
    # Instead of Vercel: any command that publishes the folder, e.g.
    #   wrangler pages deploy {dir} --project-name achtrack
    #   netlify deploy --dir {dir} --prod
    #   rsync -a --delete {dir}/ me@server:/var/www/achtrack/
    publish_command: str = ""
    sync_command: str = ""                 # optional, e.g. rclone copy {dir} gdrive:AchTrack
    # Shared folder where your machines exchange libraries (Dropbox, Drive, Syncthing, ...).
    sync_dir: str = ""
    sync_interval: float = 300.0
    # For folders your system can't sync itself (e.g. iCloud on Linux): fetch before merging,
    # upload after writing. {dir} is the shared folder.
    sync_pull_command: str = ""            # e.g. rclone copy icloud:AchTrack {dir}
    sync_push_command: str = ""            # e.g. rclone copy {dir} icloud:AchTrack
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
