"""Windows as the machine games run on directly, with no Wine prefix in between.

Everything a Wine prefix imitates - AppData, Documents, Public - is simply there. This module
says where, so the rest of AchTrack can treat the real folders the way it treats a prefix's.
"""
from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

IS_WINDOWS = os.name == "nt"

# Known-folder ids: Documents and Saved Games can be moved (OneDrive does it), so ask Windows.
FOLDER_IDS = {
    "documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    "saved_games": "{4C5C32FF-BB9D-43B0-B5B4-2D72E54EAAA4}",
    "locallow": "{A520A1A4-1780-4FF6-BD18-167343C5AF16}",
}


class _GUID(ctypes.Structure):
    _fields_ = [("a", ctypes.c_uint32), ("b", ctypes.c_uint16), ("c", ctypes.c_uint16), ("d", ctypes.c_ubyte * 8)]


def known_folder(name: str) -> Path | None:
    """One of Windows' movable folders, wherever it currently is."""
    if not IS_WINDOWS:
        return None
    try:
        guid = _GUID()
        ctypes.windll.ole32.CLSIDFromString(ctypes.c_wchar_p(FOLDER_IDS[name]), ctypes.byref(guid))
        out = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out)) != 0:
            return None
        path = out.value
        ctypes.windll.ole32.CoTaskMemFree(out)
        return Path(path) if path else None
    except (OSError, AttributeError, KeyError):
        return None


def _env(name: str, fallback: Path) -> Path:
    value = os.environ.get(name)
    return Path(value) if value else fallback


def profile() -> Path:
    return _env("USERPROFILE", Path.home())


def public() -> Path:
    return _env("PUBLIC", Path(os.environ.get("SystemDrive", "C:") + "\\") / "Users" / "Public")


def appdata() -> Path:
    return _env("APPDATA", profile() / "AppData" / "Roaming")


def local_appdata() -> Path:
    return _env("LOCALAPPDATA", profile() / "AppData" / "Local")


def documents() -> Path:
    return known_folder("documents") or profile() / "Documents"


def saved_games() -> Path:
    return known_folder("saved_games") or profile() / "Saved Games"


def locallow() -> Path:
    return known_folder("locallow") or profile() / "AppData" / "LocalLow"


def emulator_dirs() -> dict:
    """The folders crack emulators write to, keyed the way scanner.WIN_PATTERNS is."""
    dirs = {
        "appdata": [appdata()],
        "local": [local_appdata()],
        "documents": [documents(), profile() / "Documents"],
        "public": [public() / "Documents"],
        "programdata": [_env("PROGRAMDATA", Path(os.environ.get("SystemDrive", "C:") + "\\") / "ProgramData")],
    }
    out = {}
    for kind, paths in dirs.items():
        seen = []
        for path in paths:
            if path.is_dir() and path not in seen:
                seen.append(path)
        out[kind] = seen
    return out


def save_roots() -> list:
    """Where Windows games keep their saves. All of these are shared with everything else on the
    machine, so a search in them has to stay inside folders that belong to the game."""
    roots = []
    for path in (documents(), profile() / "Documents", saved_games(), appdata(), local_appdata(), locallow(),
                 public() / "Documents"):
        if path.is_dir() and path not in roots:
            roots.append(path)
    return roots


def steam_root() -> Path | None:
    """Where the Steam client is installed, from the place Steam itself records it."""
    if not IS_WINDOWS:
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            value, _kind = winreg.QueryValueEx(key, "SteamPath")
        if value:
            return Path(value)
    except (OSError, ImportError):
        pass
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if base and (Path(base) / "Steam").is_dir():
            return Path(base) / "Steam"
    return None


def is_system_path(path: str) -> bool:
    """Part of Windows itself rather than something you installed."""
    low = os.path.normcase(path)
    root = os.path.normcase(os.environ.get("SystemRoot") or r"C:\Windows")
    return low.startswith(root + os.sep)


def frozen() -> bool:
    """Running as the ready-made AchTrack.exe rather than from a Python install."""
    return bool(getattr(sys, "frozen", False))
