"""Finds Wine/Proton prefixes and the emulator achievement files inside them."""
from __future__ import annotations

import fnmatch
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .config import _xdg


@dataclass(frozen=True)
class StateFile:
    path: str
    appid: int
    emulator: str


# (glob relative to a Windows folder, index of the path part holding the appid, emulator)
WIN_PATTERNS = {
    "appdata": [
        ("Goldberg SteamEmu Saves/*/achievements.json", 1, "Goldberg"),
        ("GSE Saves/*/achievements.json", 1, "GBE"),
        ("Steam/CODEX/*/achievements.ini", 2, "CODEX"),
        ("EMPRESS/*/remote/*/achievements.json", 1, "EMPRESS"),
        ("OnlineFix/*/Stats/Achievements.ini", 1, "OnlineFix"),
    ],
    "public": [
        ("Steam/CODEX/*/achievements.ini", 2, "CODEX"),
        ("Steam/RUNE/*/achievements.ini", 2, "RUNE"),
        ("EMPRESS/*/remote/*/achievements.json", 1, "EMPRESS"),
        ("OnlineFix/*/Stats/Achievements.ini", 1, "OnlineFix"),
    ],
    "local": [
        ("SKIDROW/*/SteamEmu/UserStats/achiev.ini", 1, "SKIDROW"),
    ],
    "documents": [
        ("SKIDROW/*/SteamEmu/UserStats/achiev.ini", 1, "SKIDROW"),
        ("Steam/CODEX/*/achievements.ini", 2, "CODEX"),
    ],
    "programdata": [
        ("Steam/*/*/stats/achievements.ini", 2, "3DM"),
    ],
}

# Native Linux builds of Goldberg/GBE save into XDG data.
LINUX_PATTERNS = [
    ("Goldberg SteamEmu Saves/*/achievements.json", 1, "Goldberg"),
    ("GSE Saves/*/achievements.json", 1, "GBE"),
]

STATE_NAMES = {"achievements.json", "achievements.ini", "achiev.ini"}
FAKE_APPIDS = {0, 480}  # 480 = Spacewar, used as a placeholder by some cracks


def ci_glob(base: Path, pattern: str):
    """Case-insensitive glob - Windows games don't agree on folder casing."""
    parts = pattern.lower().split("/")

    def walk(cur: str, i: int):
        try:
            entries = list(os.scandir(cur))
        except OSError:
            return
        last = i == len(parts) - 1
        for entry in entries:
            if not fnmatch.fnmatchcase(entry.name.lower(), parts[i]):
                continue
            try:
                if last and entry.is_file():
                    yield entry.path
                elif not last and entry.is_dir():
                    yield from walk(entry.path, i + 1)
            except OSError:
                continue

    yield from walk(str(base), 0)


def _match(base: Path, pattern: str, index: int, emulator: str):
    for path in ci_glob(base, pattern):
        part = Path(path).relative_to(base).parts[index]
        if part.isdigit() and int(part) not in FAKE_APPIDS:
            yield StateFile(path, int(part), emulator)


def is_prefix(path: Path) -> bool:
    return (path / "drive_c").is_dir()


def windows_dirs(prefix: Path) -> dict:
    drive_c = prefix / "drive_c"
    dirs = {"appdata": [], "local": [], "documents": [], "public": [], "programdata": [drive_c / "ProgramData"]}
    try:
        users = [e for e in os.scandir(drive_c / "users") if e.is_dir()]
    except OSError:
        users = []
    for user in users:
        home = Path(user.path)
        if user.name.lower() == "public":
            dirs["public"] += [home / "Documents", home / "Public Documents"]
            continue
        dirs["appdata"] += [home / "AppData" / "Roaming", home / "Application Data"]
        dirs["local"] += [home / "AppData" / "Local", home / "Local Settings" / "Application Data"]
        dirs["documents"] += [home / "Documents", home / "My Documents"]
    return {k: [p for p in v if p.is_dir()] for k, v in dirs.items()}


def steam_roots() -> list:
    home = Path.home()
    return [
        home / ".local/share/Steam",
        home / ".steam/steam",
        home / ".steam/root",
        home / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
        home / ".var/app/com.valvesoftware.Steam/data/Steam",
        home / "snap/steam/common/.local/share/Steam",
    ]


def detect_steam_id() -> str | None:
    """SteamID64 of the account logged into the local Steam client (most recent login wins)."""
    fallback = None
    for root in steam_roots():
        try:
            vdf = (root / "config" / "loginusers.vdf").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for steam_id, body in re.findall(r'"(7656119\d{10})"\s*\{([^}]*)\}', vdf):
            if re.search(r'"MostRecent"\s*"1"', body, re.IGNORECASE):
                return steam_id
            fallback = fallback or steam_id
    return fallback


def steam_libraries() -> list:
    libs = []
    for root in steam_roots():
        if not root.is_dir():
            continue
        libs.append(root)
        try:
            vdf = (root / "steamapps/libraryfolders.vdf").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        libs += [Path(p.replace("\\\\", "\\")) for p in re.findall(r'"path"\s+"([^"]+)"', vdf)]
    return libs


HEROIC_CONFIG_DIRS = (
    ".config/heroic",
    ".var/app/com.heroicgameslauncher.hgl/config/heroic",  # Flatpak
)


def _heroic_json_files():
    home = Path.home()
    for rel in HEROIC_CONFIG_DIRS:
        base = home / rel
        if not base.is_dir():
            continue
        yield base / "config.json"
        yield base / "sideload_apps" / "library.json"
        yield from sorted((base / "GamesConfig").glob("*.json"))


def _collect(data, keys: set, out: list) -> None:
    if isinstance(data, dict):
        for k, v in data.items():
            if k in keys and isinstance(v, str) and v.strip():
                out.append(v.strip())
            else:
                _collect(v, keys, out)
    elif isinstance(data, list):
        for v in data:
            _collect(v, keys, out)


def _heroic_values(keys: set) -> list:
    values: list = []
    for path in _heroic_json_files():
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        _collect(data, keys, values)
    return [os.path.expanduser(v) for v in values]


def heroic_prefixes() -> list:
    """Every Wine/Proton prefix Heroic is configured to use (per game and the default)."""
    return [Path(p) for p in _heroic_values({"winePrefix", "defaultWinePrefix"})]


def heroic_game_dirs() -> list:
    """Install folders of games added to Heroic by hand (sideloaded) - where cracked games live."""
    dirs = []
    for value in _heroic_values({"executable", "folder_name", "install_path"}):
        path = Path(value)
        folder = path.parent if path.suffix.lower() in (".exe", ".sh", ".x86_64", ".bat") else path
        if folder.is_dir() and folder != Path.home() and folder not in dirs:
            dirs.append(folder)
    return dirs


def _as_prefix(path: Path) -> Path | None:
    """Wine prefixes have drive_c directly; Proton/umu prefixes keep it under pfx/."""
    if is_prefix(path):
        return path
    if is_prefix(path / "pfx"):
        return path / "pfx"
    return None


def _prefixes_under(root: Path, depth: int):
    if not root.is_dir():
        return
    prefix = _as_prefix(root)
    if prefix:
        yield prefix
        return
    if depth <= 0:
        return
    try:
        children = [Path(e.path) for e in os.scandir(root) if e.is_dir()]
    except OSError:
        return
    for child in children:
        yield from _prefixes_under(child, depth - 1)


def find_prefixes(extra_dirs=()) -> list:
    home = Path.home()
    candidates = [home / ".wine"]
    for lib in steam_libraries():
        compat = lib / "steamapps" / "compatdata"
        try:
            candidates += [Path(e.path) / "pfx" for e in os.scandir(compat) if e.is_dir()]
        except OSError:
            pass
    for prefix in heroic_prefixes():
        found = _as_prefix(prefix)
        if found:
            candidates.append(found)
    roots = [
        (home / "Library/Application Support/CrossOver/Bottles", 1),   # macOS
        (home / ".cxoffice", 1),                                        # CrossOver on Linux
        (home / "Games", 2),                      # Lutris default + umu / custom
        (home / "Games/Heroic/Prefixes", 2),      # Heroic: Prefixes/default/<game>[/pfx]
        (home / ".local/share/bottles/bottles", 1),
        (home / ".var/app/com.usebottles.bottles/data/bottles/bottles", 1),
        (home / ".var/app/net.lutris.Lutris/data/lutris/prefixes", 1),
        (home / ".local/share/wineprefixes", 1),  # winetricks
        (home / "Faugus", 1),
        (home / ".local/share/faugus-launcher/prefixes", 1),
        (home / "Prefixes", 1),
    ]
    roots += [(Path(os.path.expanduser(d)), 3) for d in extra_dirs if d]
    for root, depth in roots:
        candidates += list(_prefixes_under(root, depth))

    seen, prefixes = set(), []
    for cand in candidates:
        try:
            real = cand.resolve()
        except OSError:
            continue
        real = _as_prefix(real)
        if real is not None and real not in seen:
            seen.add(real)
            prefixes.append(real)
    return prefixes


def _walk_game_dir(game_dir: Path, max_depth: int = 5):
    """Goldberg with local_save.txt keeps saves next to the game: <dir>/<save>/<appid>/achievements.json."""
    base_depth = len(game_dir.parts)
    for root, dirs, files in os.walk(game_dir):
        depth = len(Path(root).parts) - base_depth
        if depth >= max_depth:
            dirs[:] = []
        parent = os.path.basename(root)
        if not parent.isdigit() or int(parent) in FAKE_APPIDS:
            continue
        for name in files:
            if name.lower() in STATE_NAMES:
                yield StateFile(os.path.join(root, name), int(parent), "Goldberg (local save)")


def find_state_files(extra_dirs=(), game_dirs=()) -> list:
    found: dict = {}

    def add(sf: StateFile):
        found.setdefault(os.path.realpath(sf.path), sf)

    for prefix in find_prefixes(extra_dirs):
        for kind, bases in windows_dirs(prefix).items():
            for base in bases:
                for pattern, index, emulator in WIN_PATTERNS[kind]:
                    for sf in _match(base, pattern, index, emulator):
                        add(sf)

    linux_data = _xdg("XDG_DATA_HOME", ".local/share")
    for pattern, index, emulator in LINUX_PATTERNS:
        for sf in _match(linux_data, pattern, index, emulator):
            add(sf)

    for game_dir in list(game_dirs) + [str(d) for d in heroic_game_dirs()]:
        if game_dir and os.path.isdir(game_dir):
            for sf in _walk_game_dir(Path(game_dir)):
                add(sf)

    return [StateFile(real, sf.appid, sf.emulator) for real, sf in found.items()]


# --- game folder helpers ---------------------------------------------------

APPID_INI_RE = re.compile(r"^\s*(?:real)?app_?id\s*=\s*\"?(\d+)", re.IGNORECASE | re.MULTILINE)
APPID_INI_FILES = ("steam_emu.ini", "onlinefix.ini", "cream_api.ini", "smartsteamemu.ini", "steam_api.ini",
                   "codex.ini", "rune.ini", "empress.ini", "ds.ini")


def _ci_children(directory: str) -> dict:
    try:
        return {e.name.lower(): e.path for e in os.scandir(directory)}
    except OSError:
        return {}


def appid_in_dir(directory: str) -> int | None:
    """Reads the appid a crack was configured with (steam_appid.txt, steam_emu.ini, ...)."""
    children = _ci_children(directory)
    candidates = [children.get("steam_appid.txt")]
    settings = children.get("steam_settings")
    if settings:
        candidates.append(_ci_children(settings).get("steam_appid.txt"))
    for path in candidates:
        if not path:
            continue
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        m = re.match(r"\d+", text)
        if m and int(m.group()) not in FAKE_APPIDS:
            return int(m.group())
    for name in APPID_INI_FILES:
        path = children.get(name)
        if not path:
            continue
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in APPID_INI_RE.finditer(text):
            if int(m.group(1)) not in FAKE_APPIDS:
                return int(m.group(1))
    return None


def detect_appid_in_tree(game_dir: str, max_depth: int = 4, max_dirs: int = 4000) -> int | None:
    """Looks through a game folder for the crack's appid config (used by the Add Game dialog)."""
    if not game_dir or not os.path.isdir(game_dir):
        return None
    base_depth = len(Path(game_dir).parts)
    for visited, (root, dirs, _files) in enumerate(os.walk(game_dir)):
        if visited >= max_dirs:
            break
        if len(Path(root).parts) - base_depth >= max_depth:
            dirs[:] = []
        appid = appid_in_dir(root)
        if appid:
            return appid
    return None


def find_local_schema(game_dir: str, max_depth: int = 4) -> Path | None:
    """Goldberg's steam_settings/achievements.json holds the full achievement schema."""
    if not game_dir or not os.path.isdir(game_dir):
        return None
    base_depth = len(Path(game_dir).parts)
    for root, dirs, files in os.walk(game_dir):
        if len(Path(root).parts) - base_depth >= max_depth:
            dirs[:] = []
        if os.path.basename(root).lower() == "steam_settings":
            for name in files:
                if name.lower() == "achievements.json":
                    return Path(root) / name
    return None
