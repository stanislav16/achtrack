"""Reading game save files in common engine formats, and finding which files are a game's saves.

Every reader turns a save into a flat {path: value} dict ("Progress.Chapter": 6,
"Bosses[3]": "SoulOfCinder", "Inventory.Stones.__len__": 23) that the matcher can compare
between two saves. Supported:
  * Unreal Engine SaveGame (GVAS), also zlib/gzip-compressed
  * JSON (Unity JsonUtility, Easy Save 3 unencrypted, most indie games)
  * XML (.NET/Unity XmlSerializer, many older games)
  * INI / key=value text
  * .NET BinaryFormatter (common in Unity games), BSON, MessagePack, plist, SQLite
  * RPG Maker MV/MZ saves, base64-wrapped saves, zlib/gzip behind a custom header
  * Unity PlayerPrefs, which Wine keeps in the prefix's user.reg (and ~/.config/unity3d natively)
Encrypted or proprietary binary saves are reported as unreadable.
"""
from __future__ import annotations

import base64
import gzip
import json
import plistlib
import sqlite3
import os
import re
import struct
import time
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

from . import formats, readers, winhost

MAX_SAVE_BYTES = 64 * 1024 * 1024
MAX_FIELDS = 200_000
MAX_LIST = 5_000


# Kept in a decoded save next to its values when a reader made it: which reader, and what the
# reader itself says about each achievement.
READER_KEY = "__reader__"
RULING_KEY = "__achievement__."
PROGRESS_KEY = "__progress__."
FUZZY_LIMIT = 1500      # above this many values, comparing names with descriptions is guesswork


class Unreadable(Exception):
    pass


# --- flattening ----------------------------------------------------------------------------------

def flatten(obj, prefix: str = "", out: dict | None = None) -> dict:
    out = {} if out is None else out
    if len(out) >= MAX_FIELDS:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            flatten(v, f"{prefix}.{k}" if prefix else str(k), out)
    elif isinstance(obj, (list, tuple)):
        out[f"{prefix}.__len__"] = len(obj)
        for i, v in enumerate(obj[:MAX_LIST]):
            flatten(v, f"{prefix}[{i}]", out)
    elif isinstance(obj, (bool, int, float, str)) or obj is None:
        if isinstance(obj, float) and obj.is_integer() and abs(obj) < 2 ** 53:
            obj = int(obj)
        out[prefix or "value"] = obj
    return out


# --- Unreal Engine GVAS ----------------------------------------------------------------------------

FIXED_STRUCTS = {"Vector", "Rotator", "Quat", "Vector2D", "Vector4", "LinearColor", "Color", "Guid", "DateTime",
                 "Timespan", "IntPoint", "IntVector", "Box", "Box2D", "Plane", "Transform", "Matrix"}


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise Unreadable("truncated Unreal save")
        chunk = self.data[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def unpack(self, fmt: str):
        return struct.unpack("<" + fmt, self.take(struct.calcsize("<" + fmt)))[0]

    def fstring(self) -> str:
        n = self.unpack("i")
        if n == 0:
            return ""
        if n > 0:
            if n > 65536:
                raise Unreadable("bad string in Unreal save")
            return self.take(n).decode("utf-8", "replace").rstrip("\0")
        if -n > 65536:
            raise Unreadable("bad string in Unreal save")
        return self.take(-n * 2).decode("utf-16-le", "replace").rstrip("\0")

    def skip_guid(self) -> None:
        if self.unpack("B"):
            self.take(16)


SIMPLE = {
    "IntProperty": "i", "Int64Property": "q", "UInt32Property": "I", "UInt64Property": "Q",
    "Int16Property": "h", "UInt16Property": "H", "Int8Property": "b", "FloatProperty": "f", "DoubleProperty": "d",
}
STRINGY = {"StrProperty", "NameProperty", "ObjectProperty", "SoftObjectProperty", "ClassProperty"}


def _gvas_element(r: _Reader, kind: str, struct_name: str = "", elem_size: int = 0):
    if kind in SIMPLE:
        return r.unpack(SIMPLE[kind])
    if kind in STRINGY or kind == "EnumProperty":
        return r.fstring()
    if kind == "BoolProperty":
        return bool(r.unpack("B"))
    if kind == "ByteProperty":
        return r.unpack("B")
    if kind == "StructProperty":
        if struct_name in FIXED_STRUCTS:
            r.take(elem_size)
            return None
        return _gvas_properties(r)
    raise Unreadable(f"unsupported element type {kind}")


def _gvas_value(r: _Reader, kind: str, size: int):
    if kind == "BoolProperty":
        value = bool(r.unpack("B"))
        r.skip_guid()
        return value
    if kind == "StructProperty":
        struct_name = r.fstring()
        r.take(16)
        r.skip_guid()
        end = r.pos + size
        value = None if struct_name in FIXED_STRUCTS else _safe(lambda: _gvas_properties(r, end))
        r.pos = end
        return value
    if kind in ("ByteProperty", "EnumProperty"):
        enum = r.fstring()
        r.skip_guid()
        end = r.pos + size
        if kind == "ByteProperty" and (enum == "None" or size == 1):
            value = r.unpack("B")
        else:
            value = r.fstring()
        r.pos = end
        return value
    if kind == "ArrayProperty":
        inner = r.fstring()
        r.skip_guid()
        end = r.pos + size
        value = _safe(lambda: _gvas_array(r, inner, size))
        r.pos = end
        return value
    if kind == "SetProperty":
        inner = r.fstring()
        r.skip_guid()
        end = r.pos + size

        def read_set():
            r.unpack("i")  # removed entries
            return [_gvas_element(r, inner) for _ in range(min(r.unpack("i"), MAX_LIST))]

        value = _safe(read_set)
        r.pos = end
        return value
    if kind == "MapProperty":
        key_kind, value_kind = r.fstring(), r.fstring()
        r.skip_guid()
        end = r.pos + size

        def read_map():
            r.unpack("i")
            out = {}
            for _ in range(min(r.unpack("i"), MAX_LIST)):
                k = _gvas_element(r, key_kind)
                out[str(k) if not isinstance(k, dict) else f"#{len(out)}"] = _gvas_element(r, value_kind)
            return out

        value = _safe(read_map)
        r.pos = end
        return value
    r.skip_guid()
    end = r.pos + size
    value = None
    if kind in SIMPLE and size == struct.calcsize("<" + SIMPLE[kind]):
        value = r.unpack(SIMPLE[kind])
    elif kind in STRINGY:
        value = _safe(r.fstring)
    r.pos = end
    return value


def _gvas_array(r: _Reader, inner: str, size: int) -> list:
    count = r.unpack("i")
    if count < 0 or count > 10_000_000:
        raise Unreadable("bad array")
    if inner == "StructProperty":
        r.fstring()                     # property name
        r.fstring()                     # "StructProperty"
        inner_size = r.unpack("q")      # size + array index
        struct_name = r.fstring()
        r.take(16)
        r.skip_guid()
        if struct_name in FIXED_STRUCTS:
            r.take(inner_size & 0xFFFFFFFF)
            return [None] * min(count, MAX_LIST)
        return [_gvas_properties(r) for _ in range(min(count, MAX_LIST))]
    if inner == "ByteProperty" and size - 4 == count:
        return list(r.take(count)[:MAX_LIST])
    return [_gvas_element(r, inner) for _ in range(min(count, MAX_LIST))]


def _safe(fn):
    try:
        return fn()
    except (Unreadable, struct.error, UnicodeError, RecursionError):
        return None


def _gvas_properties(r: _Reader, end: int | None = None) -> dict:
    props: dict = {}
    while r.pos < len(r.data) and (end is None or r.pos < end):
        name = r.fstring()
        if name in ("None", ""):
            break
        kind = r.fstring()
        size = r.unpack("i")
        index = r.unpack("i")
        value = _gvas_value(r, kind, size)
        key = name if index == 0 else f"{name}[{index}]"
        props[key] = value
    return props


def parse_gvas(data: bytes) -> dict:
    r = _Reader(data)
    if r.take(4) != b"GVAS":
        raise Unreadable("not an Unreal save")
    save_version = r.unpack("i")
    r.unpack("i")                       # UE4 package version
    if save_version >= 3:
        r.unpack("i")                   # UE5 package version
    r.take(6)                           # engine major/minor/patch
    r.unpack("I")                       # changelist
    r.fstring()                         # branch
    r.unpack("i")                       # custom version format
    count = r.unpack("i")
    if not 0 <= count < 10_000:
        raise Unreadable("bad Unreal save header")
    r.take(count * 20)
    save_class = r.fstring()
    start = r.pos
    try:
        props = _gvas_properties(r)
    except (Unreadable, struct.error):
        # Some UE 5.4+ saves carry one extra flag byte before the properties.
        r.pos = start + 1
        props = _gvas_properties(r)
    return {"__class__": save_class, **props}


# --- other formats ---------------------------------------------------------------------------------

def _xml_to_obj(elem: ET.Element):
    children = list(elem)
    if not children and not elem.attrib:
        return _scalar(elem.text or "")
    out: dict = {k: _scalar(v) for k, v in elem.attrib.items()}
    for child in children:
        tag = child.tag.split("}")[-1]
        value = _xml_to_obj(child)
        if tag in out:
            if not isinstance(out[tag], list):
                out[tag] = [out[tag]]
            out[tag].append(value)
        else:
            out[tag] = value
    if elem.text and elem.text.strip() and children:
        out["#text"] = _scalar(elem.text)
    return out


def _scalar(text: str):
    s = text.strip()
    low = s.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        return s


def _parse_ini(text: str) -> dict:
    out: dict = {}
    section = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] in ";#":
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
        elif "=" in line:
            k, v = line.split("=", 1)
            out[f"{section}.{k.strip()}" if section else k.strip()] = _scalar(v)
    return out


def _decode_text(data: bytes) -> str | None:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = data.decode("utf-16", "replace")
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return None
    sample = text[:4000]
    printable = sum(ch.isprintable() or ch in "\r\n\t" for ch in sample)
    return text if sample and printable / len(sample) > 0.97 else None


def _decompressed(data: bytes):
    """zlib/gzip streams at the start or behind a small custom header."""
    offsets = list(range(0, 20, 4))
    for i in range(min(len(data) - 2, 64)):
        if (data[i] == 0x78 and data[i + 1] in (0x01, 0x5E, 0x9C, 0xDA)) or data[i:i + 2] == b"\x1f\x8b":
            offsets.append(i)
    seen = set()
    for offset in offsets:
        if offset in seen:
            continue
        seen.add(offset)
        chunk = data[offset:]
        for fn in (zlib.decompress, gzip.decompress):
            try:
                out = fn(chunk)
            except (zlib.error, OSError, EOFError, ValueError):
                continue
            if out:
                yield out
                return


PARSE_ERRORS = (Unreadable, ValueError, struct.error, IndexError, KeyError, RecursionError, UnicodeError,
                plistlib.InvalidFileException, OverflowError)
B64_RE = re.compile(r"[A-Za-z0-9+/=_-]{24,}")


def _parse_blob(blob: bytes, depth: int = 0):
    """(format, flat) for one in-memory save, or None."""
    attempts = [
        ("Unreal Engine", lambda: parse_gvas(blob) if blob[:4] == b"GVAS" else None),
        (".NET BinaryFormatter", lambda: formats.parse_nrbf(blob) if formats.is_nrbf(blob) else None),
        ("plist", lambda: formats.parse_plist(blob)),
        ("BSON", lambda: formats.parse_bson(blob)),
    ]
    for name, fn in attempts:
        try:
            value = fn()
        except PARSE_ERRORS:
            continue
        if value:
            return name, flatten(value)

    text = _decode_text(blob)
    if text is None:
        try:
            return "MessagePack", flatten(formats.parse_msgpack(blob))
        except PARSE_ERRORS:
            return None
    stripped = text.strip()
    if stripped[:1] in "{[":
        try:
            return "JSON", flatten(json.loads(stripped))
        except ValueError:
            pass
    if stripped.startswith("<"):
        try:
            root = ET.fromstring(stripped)
            if root.tag == "unity_prefs":
                return "Unity PlayerPrefs", flatten(formats.unity_linux_prefs(root))
            return "XML", flatten({root.tag.split("}")[-1]: _xml_to_obj(root)})
        except ET.ParseError:
            pass
    if depth == 0 and B64_RE.fullmatch(re.sub(r"\s", "", stripped)):
        compact = re.sub(r"\s", "", stripped)
        try:
            text_json = formats.lzstring_base64(compact)
            return "RPG Maker", flatten(json.loads(text_json))
        except PARSE_ERRORS:
            pass
        try:
            decoded = base64.b64decode(compact + "=" * (-len(compact) % 4), altchars=b"-_" if "-" in compact or "_" in compact else None)
        except (ValueError, TypeError):
            decoded = b""
        for inner in [decoded, *_decompressed(decoded)] if decoded else []:
            found = _parse_blob(inner, depth + 1)
            if found:
                return f"{found[0]} (base64)", found[1]
    if sum(1 for line in text.splitlines()[:200] if "=" in line) >= 3:
        return "INI", flatten(_parse_ini(text))
    return None


def load_save(path: str) -> tuple:
    """Returns (format name, flat dict). Raises Unreadable for encrypted/unknown files."""
    if os.path.basename(path).lower() == "user.reg":
        return "Unity PlayerPrefs (registry)", parse_user_reg(Path(path))
    size = os.path.getsize(path)
    if size > MAX_SAVE_BYTES:
        raise Unreadable("too large")
    data = Path(path).read_bytes()
    if not data:
        raise Unreadable("empty file")
    theirs = readers.try_read(path, data)        # a reader you installed knows this game's format
    if theirs:
        name, values, rulings = theirs
        flat = flatten(values)
        flat[READER_KEY] = name
        for api_name, ruling in (rulings or {}).items():
            if isinstance(ruling, tuple):
                flat[f"{PROGRESS_KEY}{api_name}"] = f"{ruling[0]}/{ruling[1]}"
                ruling = ruling[1] > 0 and ruling[0] >= ruling[1]
            flat[f"{RULING_KEY}{api_name}"] = bool(ruling)
        return f"{name} (reader)", flat
    if data.startswith(b"SQLite format 3\0"):
        try:
            return "SQLite", flatten(formats.parse_sqlite(path))
        except sqlite3.Error as e:
            raise ValueError(f"database busy: {e}")   # retried on the next poll
    for attempt, blob in enumerate([data, *_decompressed(data)]):
        found = _parse_blob(blob)
        if found:
            return found[0] + ("" if attempt == 0 else " (compressed)"), found[1]
    raise Unreadable("encrypted or unknown binary format")


# --- Unity PlayerPrefs in Wine's registry ----------------------------------------------------------

REG_SKIP = ("software\\\\wine", "software\\\\microsoft", "software\\\\classes", "software\\\\policies",
            "software\\\\valve", "control panel", "environment", "system", "volatile", "keyboard", "printers")


def _reg_value(raw: str):
    if raw.startswith('"'):
        return raw.strip('"').replace('\\"', '"')
    if raw.startswith("dword:"):
        value = int(raw[6:], 16)
        return value - 2 ** 32 if value >= 2 ** 31 else value
    m = re.match(r"hex(?:\((\w+)\))?:(.*)", raw)
    if m:
        try:
            blob = bytes.fromhex(m.group(2).replace(",", "").replace("\\", "").replace(" ", ""))
        except ValueError:
            return None
        if blob.endswith(b"\0"):                           # Unity strings: UTF-8 + terminator
            try:
                decoded = blob.rstrip(b"\0").decode("utf-8")
                if decoded.isprintable():
                    return decoded
            except UnicodeDecodeError:
                pass
        if len(blob) == 8:
            number = struct.unpack("<d", blob)[0]           # Unity floats: 8-byte doubles
            if number == number and abs(number) < 1e12:
                return int(number) if float(number).is_integer() else number
    return None


def parse_user_reg(path: Path) -> dict:
    """Game-owned keys under HKCU\\Software (where Unity keeps PlayerPrefs)."""
    text = path.read_text(encoding="utf-8", errors="replace").replace("\\\n  ", "")
    out: dict = {}
    section = None
    for line in text.splitlines():
        if line.startswith("["):
            name = line[1:line.index("]")] if "]" in line else ""
            low = name.lower()
            section = name if low.startswith("software\\\\") and not low.startswith(REG_SKIP) else None
        elif section and line.startswith('"') and "=" in line:
            key, _, raw = line.partition("=")
            key = re.sub(r"_h\d+$", "", key.strip('"'))       # Unity appends a hash to each key
            value = _reg_value(raw.strip())
            if value is not None:
                out[f"{section.replace(chr(92) * 2, '.')}.{key}"] = value
    return out


# --- finding a game's saves ------------------------------------------------------------------------

SKIP_DIR_NAMES = {
    "microsoft", "temp", "tmp", "nvidia", "nvidia corporation", "d3dscache", "cache", "caches", "shadercache",
    "shadercaches", "logs", "log", "crashes", "crashreportclient", "webcache", "packages", "config", "configs",
    "cef", "webview", "gpucache", "dxcache", "vulkan", "mesa_shader_cache", "fontcache", "unrealengine",
    "epicgameslauncher", "goldberg steamemu saves", "gse saves", "codex", "rune", "empress", "onlinefix", "skidrow",
    "wine", "windows", "programs", "start menu", "recent", "inetcache", "history",
    "content", "paks", "binaries", "engine", "movies", "shaders", "plugins", "__pycache__", "redist", "_commonredist",
}
SKIP_EXTENSIONS = {
    ".log", ".tmp", ".dmp", ".mdmp", ".dxvk-cache", ".pdb", ".dll", ".exe", ".png", ".jpg", ".jpeg", ".bmp", ".dds",
    ".ttf", ".otf", ".lock", ".etl", ".pak", ".ucas", ".utoc", ".sig", ".mp4", ".bik", ".bk2", ".wav", ".ogg",
    ".lnk", ".url", ".ico", ".cache", ".shader", ".pso", ".upipelinecache", ".ushaderbytecode",
    ".ushaderprecache", ".ushadercache", ".stablepc", ".shk", ".bin_cache", ".dxbc", ".spv", ".nvph", ".fossil",
}
# Engine data folders and files that live next to saves but aren't saves.
NOT_SAVE_NAMES = ("d3ddriverbytecodeblob", "shadercache", "pipelinecache", "gameusersettings", "engine.ini",
                  "input.ini", "scalability", "crashreport", "manifest",
                  # settings, not progress: readable, but nothing in them says what you have done
                  "settings", "keybind", "keymap", "controls.", "graphics.", "audio.", "video.", "display.")
SAVE_DIR_RE = re.compile(r"(^|[/\\])(save|saves|savegame|savegames|savedata|savefiles?|profiles?|slots?)([/\\]|$)",
                         re.IGNORECASE)


def prefix_save_roots(prefix: str) -> list:
    """Folders inside a Wine prefix where Windows games keep saves."""
    roots = []
    users = Path(prefix) / "drive_c" / "users"
    try:
        entries = [e for e in os.scandir(users) if e.is_dir()]
    except OSError:
        return roots
    for entry in entries:
        home = Path(entry.path)
        if entry.name.lower() == "public":
            roots.append(home / "Documents")
            continue
        roots += [home / "AppData" / "Local", home / "AppData" / "LocalLow", home / "AppData" / "Roaming",
                  home / "Documents", home / "Saved Games"]
    return [r for r in roots if r.is_dir()]


def game_root(game_dir: str) -> str | None:
    """Top folder of an installed game, from any folder inside it (e.g. its Steamworks DLL folder)."""
    if not game_dir:
        return None
    home = os.path.expanduser("~")
    path = os.path.abspath(game_dir)
    while True:
        parent = os.path.dirname(path)
        if parent in ("/", home, path) or os.path.basename(parent).lower() in (
                "games", "common", "heroic", "games (x86)", "program files", "program files (x86)", "drive_c"):
            return path
        path = parent


def is_candidate(path: str) -> bool:
    name = os.path.basename(path).lower()
    return (os.path.splitext(name)[1] not in SKIP_EXTENSIONS
            and not any(bad in name for bad in NOT_SAVE_NAMES))


def _walk_files(root: Path, only_save_paths: bool, max_depth: int = 7):
    base = len(root.parts)
    for dirpath, dirs, files in os.walk(root):
        depth = len(Path(dirpath).parts) - base
        dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIR_NAMES and not d.startswith(".")] if depth < max_depth else []
        if only_save_paths and not SAVE_DIR_RE.search(dirpath) and "savegame" not in dirpath.lower():
            continue
        for name in files:
            path = os.path.join(dirpath, name)
            if is_candidate(path):
                yield path


def related_prefixes(prefix: str) -> list:
    """A prefix plus its Wine/Proton twin: Heroic can leave a drive_c both in <dir> and <dir>/pfx,
    and the game may write saves to either one."""
    path = Path(prefix)
    found = []
    for cand in (path, path / "pfx", path.parent if path.name == "pfx" else None):
        if cand is not None and (cand / "drive_c").is_dir() and cand not in found:
            found.append(cand)
    return [str(p) for p in found]


GAME_FOLDERS = {"my games", "saved games", "savedgames", "savegames", "saves"}


def _name_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def _is_about(folder: str, names: list) -> bool:
    """Whether a folder's name is this game's name, give or take punctuation and an edition."""
    key = _name_key(folder)
    return any(len(n) >= 4 and len(key) >= 4 and (n in key or key in n) for n in names)


def leaves_prefix(root: Path, prefix: str) -> bool:
    """Wine and CrossOver link a prefix's Documents, Desktop and Downloads to your real ones."""
    try:
        real, inside = os.path.realpath(root), os.path.realpath(prefix)
    except OSError:
        return False
    return not (real == inside or real.startswith(inside + os.sep))


def _walk_personal(root: Path, names: list):
    """Saves in a folder that is really your own Documents: only the parts that are a game's.

    Everything else in there is yours - and anything you edit while a game is running would
    otherwise look like a save that game just wrote.
    """
    try:
        top = [e for e in os.scandir(root) if e.is_dir(follow_symlinks=False) and not e.name.startswith(".")]
    except OSError:
        return
    for entry in top:
        low = entry.name.lower()
        if low in GAME_FOLDERS or _is_about(entry.name, names):
            yield from _walk_files(Path(entry.path), only_save_paths=False)
            continue
        try:                                   # Documents/<publisher>/<game>
            inner = [e for e in os.scandir(entry.path)
                     if e.is_dir(follow_symlinks=False) and _is_about(e.name, names)]
        except OSError:
            continue
        for sub in inner:
            yield from _walk_files(Path(sub.path), only_save_paths=False)


def candidate_files(prefix: str | None, game_dir: str | None, names: list | None = None) -> list:
    """Files that could be this game's saves: everything in the prefix's user folders, plus
    anything under a *save* folder inside the game's install folder.

    `names` are what the game is called. They only matter for user folders that turn out to be
    links to your real ones, where the search is kept to folders belonging to the game.
    """
    out = []
    root = game_root(game_dir) if game_dir else None
    known = [_name_key(n) for n in (names or []) + ([os.path.basename(root)] if root else []) if _name_key(n)]
    if winhost.IS_WINDOWS:
        # No prefix: these are your real folders, shared with every other program, so only the
        # parts named after the game are searched.
        for folder in winhost.save_roots():
            out += list(_walk_personal(folder, known))
    for pfx in related_prefixes(prefix) if prefix else []:
        for folder in prefix_save_roots(pfx):
            if leaves_prefix(folder, pfx):
                out += list(_walk_personal(folder, known))
            else:
                out += list(_walk_files(folder, only_save_paths=False))
        user_reg = Path(pfx) / "user.reg"
        if user_reg.is_file():
            out.append(str(user_reg))
    if root and os.path.isdir(root):
        out += list(_walk_files(Path(root), only_save_paths=True))
    unity = Path.home() / ".config" / "unity3d"     # native Linux Unity games
    if unity.is_dir():
        out += list(_walk_files(unity, only_save_paths=False, max_depth=4))
    return out


def is_stray(path: str, game_dir: str | None, names: list | None = None) -> bool:
    """A registered "save" that is really one of your own files, reached through a prefix's link
    to your Documents - something an older version could pick up while you edited it mid-game."""
    cut = path.find(os.sep + "drive_c" + os.sep)
    if cut < 0:
        return False
    prefix = path[:cut]
    if not leaves_prefix(Path(path), prefix):
        return False
    return path not in candidate_files(prefix, game_dir, names)


def changed_since(paths: list, since: float) -> list:
    out = []
    for path in paths:
        try:
            st = os.stat(path)
        except OSError:
            continue
        if st.st_mtime >= since and 0 < st.st_size <= MAX_SAVE_BYTES:
            out.append(path)
    return out


def looks_like_save(path: str) -> bool:
    """A file that is probably a save even before it changes. Unreal's "Saved" data folder
    alone doesn't count - it also holds configs, logs and shader caches."""
    if not is_candidate(path):
        return False
    low = path.lower()
    name = os.path.basename(low)
    return (low.endswith((".sav", ".save", ".es3", ".sl2", ".rpgsave", ".rmmzsave", ".dat", "user.reg"))
            or "save" in name or name == "prefs"
            or bool(SAVE_DIR_RE.search(os.path.dirname(path))) or "savegames" in low)


def now() -> float:
    return time.time()
