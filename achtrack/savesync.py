"""Moving a game's save files between your machines.

A save lives at a path that only makes sense on one machine: a Heroic prefix under /home on the
PC, a CrossOver bottle on the Mac, each with its own idea of what the Windows user is called. So
a backup doesn't record where the files were - it records what they are *relative to*:

    user/Saved Games/Hades II/Profile1.sav      the Windows user's folder in the game's prefix
    public/Documents/Steam/RUNE/1145350/remote  the shared "Public" user
    game/Saves/slot1.sav                        the game's own install folder

and restoring puts each one back under the same anchor on whichever machine it is, using the
prefix and folder AchTrack already knows the game runs from. Backups are zip files in the folder
your devices share, so they travel the same way your library does.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
import zipfile
from pathlib import Path

from . import savefiles

log = logging.getLogger(__name__)

FOLDER = "saves"
KEEP_PER_DEVICE = 5
MAX_FILE = 512 * 1024 * 1024
# Folders that hold many games' things: take the save file itself, not everything beside it.
SHARED_FOLDERS = {"documents", "my documents", "desktop", "downloads", "appdata", "local", "locallow",
                  "roaming", "saved games", "savedgames", "my games", "public", "users", "drive_c",
                  "programdata", "application data", "local settings"}
EMULATOR_STORAGE = ("remote", "local", "saves", "storage")
# Left out of a backup: noise, and settings that describe one machine's screen and keyboard.
NOT_CARRIED = (".log", ".tmp", ".dmp", ".mdmp", ".lock", ".crash")
MACHINE_SETTINGS = ("settings", "keybind", "keymap", "controls.", "graphics.", "video.", "display.")


class SaveSyncError(Exception):
    pass


# --- where things are on this machine -------------------------------------------------------------

def prefix_of(path: str) -> str:
    cut = path.find(os.sep + "drive_c" + os.sep)
    return path[:cut] if cut > 0 else ""


def windows_user(prefix: str) -> str:
    """The folder name of the Windows user in a prefix: "crossover", "steamuser", or your own."""
    users = Path(prefix) / "drive_c" / "users"
    try:
        names = [e.name for e in os.scandir(users) if e.is_dir() and e.name.lower() != "public"]
    except OSError:
        return ""
    for name in names:                                  # the one that has actually been used
        if (users / name / "AppData").is_dir():
            return name
    return names[0] if names else ""


class Layout:
    """How the anchors map to real folders for one game on this machine."""

    def __init__(self, prefix: str, game_dir: str | None):
        self.prefix = prefix or ""
        self.root = savefiles.game_root(game_dir) if game_dir else None
        drive = os.path.join(self.prefix, "drive_c") if self.prefix else ""
        user = windows_user(self.prefix) if self.prefix else ""
        self.anchors = {}
        if drive and user:
            self.anchors["user"] = os.path.join(drive, "users", user)
        if drive:
            public = next((n for n in ("Public", "public") if os.path.isdir(os.path.join(drive, "users", n))), "Public")
            self.anchors["public"] = os.path.join(drive, "users", public)
            self.anchors["c"] = drive
        if self.root:
            self.anchors["game"] = self.root

    def portable(self, path: str) -> str | None:
        """A real path as anchor/relative, or None if it sits under none of them."""
        for anchor in ("game", "user", "public", "c"):       # most specific first
            base = self.anchors.get(anchor)
            if base and (path == base or path.startswith(base + os.sep)):
                return anchor + "/" + os.path.relpath(path, base).replace(os.sep, "/")
        return None

    def real(self, portable: str) -> str | None:
        anchor, _, rest = portable.partition("/")
        base = self.anchors.get(anchor)
        parts = [p for p in rest.split("/") if p]
        if not base or not parts or any(p in ("..", ".") or "\\" in p or ":" in p for p in parts):
            return None
        return os.path.join(base, *parts)


def find_layout(db, appid: int) -> Layout:
    """The prefix and folder a game uses here, from everything AchTrack has seen of it."""
    game = db.game(appid)
    game_dir = game["game_dir"] if game is not None and game["game_dir"] and os.path.isdir(game["game_dir"]) else None
    remembered = (db.get_meta("prefixes") or {}).get(str(appid), "")
    candidates = [remembered] + [prefix_of(p) for p in db.sources_for(appid)] \
        + [prefix_of(r["path"]) for r in db.save_files(appid)] + [prefix_of(game_dir or "")]
    prefix = next((p for p in candidates if p and os.path.isdir(os.path.join(p, "drive_c"))), "")
    return Layout(prefix, game_dir)


# --- what counts as this game's saves -------------------------------------------------------------

def _carried(path: str) -> bool:
    name = os.path.basename(path).lower()
    return not name.startswith(".") and not name.endswith(NOT_CARRIED) \
        and not any(word in name for word in MACHINE_SETTINGS)


def save_places(db, appid: int) -> list:
    """Folders and files that are this game's saves here, most specific first."""
    places: list = []

    def add(path: str) -> None:
        if path not in places and os.path.exists(path):
            places.append(path)

    for row in db.save_files(appid):                     # files seen changing while the game ran
        path = row["path"]
        if os.path.basename(path).lower() == "user.reg":
            continue                                     # the registry belongs to the whole prefix
        parent = os.path.dirname(path)
        add(path if os.path.basename(parent).lower() in SHARED_FOLDERS else parent)
    for source in db.sources_for(appid):                 # a crack's stand-in for Steam Cloud
        beside = os.path.dirname(source)
        for name in EMULATOR_STORAGE:
            folder = os.path.join(beside, name)
            if os.path.isdir(folder) and any(files for _d, _s, files in os.walk(folder)):
                add(folder)
    # A folder already covered by one above it adds nothing.
    return [p for p in places if not any(p != q and p.startswith(q + os.sep) for q in places)]


def collect(db, appid: int, layout: Layout) -> list:
    """[(real path, portable path)] for every file a backup of this game would hold."""
    out, seen = [], set()
    for place in save_places(db, appid):
        walk = [(os.path.dirname(place), [], [os.path.basename(place)])] if os.path.isfile(place) else os.walk(place)
        for folder, dirs, files in walk:
            dirs[:] = [d for d in dirs if d.lower() not in ("cache", "caches", "logs", "crashes", "shadercache")]
            for name in files:
                path = os.path.join(folder, name)
                portable = layout.portable(path)
                try:
                    usable = _carried(path) and portable and os.path.isfile(path) and os.path.getsize(path) <= MAX_FILE
                except OSError:
                    usable = False
                if usable and portable not in seen:
                    seen.add(portable)
                    out.append((path, portable))
    return out


# --- backups ----------------------------------------------------------------------------------------

def store(cfg, backup_dir: Path) -> Path:
    """Where backups go: the folder your devices share, or beside your other backups if there is none."""
    shared = cfg.sync_dir.strip()
    return (Path(shared).expanduser() if shared else Path(backup_dir)) / FOLDER


def backup(db, cfg, backup_dir: Path, appid: int, device: tuple, note: str = "", keep: str = "") -> dict:
    """Packs a game's saves into the shared folder. Returns {"path", "files", "bytes"}."""
    game = db.game(appid)
    if game is None:
        raise SaveSyncError("no such game")
    layout = find_layout(db, appid)
    files = collect(db, appid, layout)
    if not files:
        raise SaveSyncError("AchTrack hasn't seen this game write a save on this machine yet - play it "
                            "and save once, then try again")
    device_id, device_name = device
    created = int(time.time())
    folder = store(cfg, backup_dir) / str(appid)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(created))
    dest, extra = folder / f"{stamp}-{device_id}.zip", 1
    while dest.exists():                    # never write over a backup, even one made this second
        extra += 1
        dest = folder / f"{stamp}-{extra}-{device_id}.zip"
    manifest = {"app": "achtrack-saves", "version": 1, "appid": appid, "name": game["name"] or "",
                "device": device_id, "device_name": device_name, "created": created, "note": note,
                "files": [{"path": portable, "size": os.path.getsize(path), "mtime": os.path.getmtime(path)}
                          for path, portable in files]}
    tmp = dest.with_suffix(".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=1))
        for path, portable in files:
            archive.write(path, "files/" + portable)
    tmp.replace(dest)
    _prune(folder, device_id, keep)
    return {"path": str(dest), "files": len(files), "bytes": dest.stat().st_size}


def _prune(folder: Path, device_id: str, keep: str = "") -> None:
    """Drops this device's oldest backups - by when they were made, and never the one in use."""
    mine = sorted(folder.glob(f"*-{device_id}.zip"), key=lambda path: (path.stat().st_mtime, path.name))
    for old in [path for path in mine[:-KEEP_PER_DEVICE] if str(path) != keep]:
        try:
            old.unlink()
        except OSError:
            pass


def read_manifest(path: Path) -> dict | None:
    try:
        with zipfile.ZipFile(path) as archive:
            data = json.loads(archive.read("manifest.json"))
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        return None
    return data if isinstance(data, dict) and data.get("app") == "achtrack-saves" else None


def backups(cfg, backup_dir: Path, appid: int) -> list:
    """Backups of a game from every device, newest first. Ones iCloud hasn't fetched are flagged."""
    folder = store(cfg, backup_dir) / str(appid)
    out = []
    try:
        entries = sorted(folder.iterdir(), reverse=True)
    except OSError:
        return out
    for path in entries:
        if path.name.startswith(".") and path.name.endswith(".icloud"):     # still in the cloud
            _fetch_from_icloud(path)
            out.append({"path": str(path), "pending": True, "name": path.name[1:-7]})
        elif path.suffix == ".zip":
            manifest = read_manifest(path)
            if manifest:
                out.append({"path": str(path), "pending": False, "created": manifest.get("created", 0),
                            "device": manifest.get("device", ""), "device_name": manifest.get("device_name", ""),
                            "files": len(manifest.get("files") or []), "bytes": path.stat().st_size,
                            "note": manifest.get("note", "")})
    out.sort(key=lambda b: b.get("created", 0), reverse=True)
    return out


def _fetch_from_icloud(placeholder: Path) -> None:
    if sys.platform == "darwin":
        try:
            subprocess.Popen(["brctl", "download", str(placeholder.parent / placeholder.name[1:-7])],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass


def restore(db, cfg, backup_dir: Path, appid: int, archive_path: str, device: tuple) -> dict:
    """Puts a backup's files where this machine keeps that game's saves.

    What is there now is backed up first, so a restore can always be taken back.
    """
    manifest = read_manifest(Path(archive_path))
    if manifest is None:
        raise SaveSyncError("that isn't a save backup AchTrack made, or it hasn't finished downloading")
    if int(manifest.get("appid") or 0) != appid:
        raise SaveSyncError("that backup belongs to a different game")
    layout = find_layout(db, appid)
    wanted = [f["path"] for f in manifest.get("files") or []]
    needs = {p.partition("/")[0] for p in wanted}
    missing = sorted(a for a in needs if a not in layout.anchors)
    if missing:
        what = {"game": "where the game is installed", "user": "which bottle or prefix the game uses",
                "public": "which bottle or prefix the game uses", "c": "which bottle or prefix the game uses"}
        raise SaveSyncError(f"AchTrack doesn't know {what.get(missing[0], missing[0])} on this machine yet. "
                            f"Start the game here once, quit it, and restore again")
    targets = [(p, layout.real(p)) for p in wanted]
    if any(real is None for _p, real in targets):
        raise SaveSyncError("the backup has a file path that can't be trusted")

    safety = ""
    if any(os.path.exists(real) for _p, real in targets) or collect(db, appid, layout):
        try:
            safety = backup(db, cfg, backup_dir, appid, device, note="before restore",
                            keep=archive_path)["path"]
        except SaveSyncError:
            safety = ""                                   # nothing known here yet, so nothing to lose
    written = 0
    with zipfile.ZipFile(archive_path) as archive:
        times = {f["path"]: f.get("mtime") for f in manifest.get("files") or []}
        for portable, real in targets:
            os.makedirs(os.path.dirname(real), exist_ok=True)
            tmp = real + ".achtrack-part"
            with archive.open("files/" + portable) as src, open(tmp, "wb") as dst:
                while chunk := src.read(1024 * 1024):
                    dst.write(chunk)
            os.replace(tmp, real)
            if times.get(portable):
                os.utime(real, (times[portable], times[portable]))
            written += 1
    return {"files": written, "safety": safety, "from": manifest.get("device_name", ""),
            "created": manifest.get("created", 0)}
