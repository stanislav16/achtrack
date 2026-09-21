"""Detects running games by inspecting /proc.

A process counts as a game when any of these hold:
  1. it was launched with ACHTRACK_APPID=<appid> in its environment
  2. its executable name matches one configured for a game in the library
  3. it loaded steam_api(64).dll / libsteam_api.so from a folder holding a crack config
     (steam_appid.txt, steam_settings/, steam_emu.ini, OnlineFix.ini, ...)
  4. its Windows .exe sits in (or just below) such a folder
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

from .scanner import appid_in_dir

STEAM_LIBS = ("steam_api64.dll", "steam_api.dll", "libsteam_api.so")
SYSTEM_PREFIXES = ("/usr/", "/bin/", "/sbin/", "/lib", "/opt/steam", "/snap/", "/nix/", "/app/", "/proc/", "/dev/")
DETECT_ATTEMPTS = 12  # a game loads steam_api shortly after starting - keep looking for ~1 minute


@dataclass
class _Proc:
    args: list
    exe: str
    env: dict
    detected: tuple | None = None   # (appid, game_dir)
    attempts: int = 0
    ignore: bool = False


def stat_fields(raw: bytes) -> tuple:
    """(state, start time) from /proc/<pid>/stat. The command name is in brackets and can itself
    contain brackets and spaces, so everything is read after the last one."""
    fields = raw.rsplit(b")", 1)[-1].split()
    return (fields[0] if fields else b"", fields[19] if len(fields) > 19 else b"")


def _read(path: str) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return b""


def _link(path: str) -> str:
    try:
        return os.readlink(path)
    except OSError:
        return ""


def _basename(arg: str) -> str:
    return re.split(r"[\\/]", arg)[-1]


def win_to_linux(arg: str, cwd: str, prefix: str) -> str | None:
    path = arg.replace("\\", "/")
    m = re.match(r"^([A-Za-z]):/(.*)$", path)
    if m:
        drive, rest = m.group(1).lower(), m.group(2)
        if drive == "z":
            return "/" + rest
        if prefix:
            if drive == "c":
                return os.path.join(prefix, "drive_c", rest)
            return os.path.join(prefix, "dosdevices", f"{drive}:", rest)
        return None
    if path.startswith("/"):
        return path
    return os.path.join(cwd, path) if cwd else None


def _prefix_of(proc: "_Proc") -> str:
    prefix = proc.env.get("WINEPREFIX", "")
    if not prefix and proc.env.get("STEAM_COMPAT_DATA_PATH"):
        prefix = os.path.join(proc.env["STEAM_COMPAT_DATA_PATH"], "pfx")
    return prefix


def _is_steam_install(path: str) -> bool:
    # Games bought on Steam already get real achievements - leave them alone.
    return "/steamapps/common/" in path.lower()


class ProcessMonitor:
    def __init__(self):
        self.uid = os.getuid()
        self.self_pid = str(os.getpid())
        self.cache: dict = {}
        self.dir_cache: dict = {}
        self.prefixes: dict = {}   # appid -> Wine prefix of its process, from the last scan

    @staticmethod
    def available() -> bool:
        return os.path.isdir("/proc/self") and os.path.exists("/proc/self/cmdline")

    def reset(self) -> None:
        self.dir_cache.clear()
        for proc in self.cache.values():
            if proc.detected is None:
                proc.attempts = 0

    def scan(self, exe_map: dict) -> dict:
        """Returns {appid: game_dir or None} for every game currently running."""
        found: dict = {}
        self.prefixes = {}
        self.seen = {}          # appid -> [(pid, state, what matched)], for `achtrack scan`
        alive = set()
        try:
            pids = [e.name for e in os.scandir("/proc") if e.name.isdigit()]
        except OSError:
            return found
        for pid in pids:
            if pid == self.self_pid:
                continue
            stat = _read(f"/proc/{pid}/stat")
            if not stat:
                continue
            state, started = stat_fields(stat)
            if state in (b"Z", b"X", b"x"):
                # Already exited, just not reaped by its launcher yet. It has no cmdline any more,
                # but a process cached while it was running would otherwise keep counting.
                continue
            key = (pid, started)
            alive.add(key)
            proc = self.cache.get(key)
            if proc is None:
                proc = self.cache[key] = self._load(pid)
            if proc.ignore:
                continue

            appid = self._env_appid(proc)
            game_dir = None
            if appid is None and exe_map:
                for name in [_basename(a).lower() for a in proc.args[:2]] + [_basename(proc.exe).lower()]:
                    if name in exe_map:
                        appid = exe_map[name]
                        break
            if appid is None:
                if proc.detected is None and proc.attempts < DETECT_ATTEMPTS:
                    proc.attempts += 1
                    proc.detected = self._detect(pid, proc)
                if proc.detected:
                    appid, game_dir = proc.detected
            if appid is not None:
                self.seen.setdefault(appid, []).append(
                    (pid, state.decode("ascii", "replace"), proc.exe or (proc.args[0] if proc.args else "?")))
                prefix = _prefix_of(proc)
                if prefix and appid not in self.prefixes:
                    self.prefixes[appid] = prefix
                found.setdefault(appid, game_dir)
                if game_dir and not found[appid]:
                    found[appid] = game_dir

        for key in list(self.cache):
            if key not in alive:
                del self.cache[key]
        return found

    def _load(self, pid: str) -> _Proc:
        try:
            if os.stat(f"/proc/{pid}").st_uid != self.uid:
                return _Proc([], "", {}, ignore=True)
        except OSError:
            return _Proc([], "", {}, ignore=True)
        raw = _read(f"/proc/{pid}/cmdline")
        if not raw:
            return _Proc([], "", {}, ignore=True)
        args = [a.decode("utf-8", "surrogateescape") for a in raw.split(b"\0") if a]
        env = {}
        for item in _read(f"/proc/{pid}/environ").split(b"\0"):
            k, sep, v = item.partition(b"=")
            if sep and k in (b"ACHTRACK_APPID", b"WINEPREFIX", b"STEAM_COMPAT_DATA_PATH"):
                env[k.decode()] = v.decode("utf-8", "surrogateescape")
        return _Proc(args, _link(f"/proc/{pid}/exe"), env)

    @staticmethod
    def _env_appid(proc: _Proc) -> int | None:
        value = proc.env.get("ACHTRACK_APPID", "").strip()
        return int(value) if value.isdigit() and int(value) > 0 else None

    def _dir_appid(self, directory: str) -> int | None:
        if directory not in self.dir_cache:
            self.dir_cache[directory] = appid_in_dir(directory)
        return self.dir_cache[directory]

    def _detect(self, pid: str, proc: _Proc) -> tuple | None:
        # Loaded steam_api library -> its folder carries the crack's config.
        maps = _read(f"/proc/{pid}/maps").decode("utf-8", "surrogateescape")
        lib_dirs = []
        for line in maps.splitlines():
            parts = line.split(None, 5)
            if len(parts) == 6 and parts[5].lower().endswith(STEAM_LIBS):
                d = os.path.dirname(parts[5])
                if d not in lib_dirs:
                    lib_dirs.append(d)
        for d in lib_dirs:
            if _is_steam_install(d):
                proc.attempts = DETECT_ATTEMPTS
                return None
            appid = self._dir_appid(d)
            if appid:
                return appid, d

        # Wine rewrites argv[0] to the Windows path of the .exe.
        prefix = _prefix_of(proc)
        cwd = _link(f"/proc/{pid}/cwd")
        exes = [a for a in proc.args[:3] if a.lower().endswith(".exe")]
        paths = [win_to_linux(a, cwd, prefix) for a in exes]
        if proc.exe and not proc.exe.startswith(SYSTEM_PREFIXES) and not exes:
            paths.append(proc.exe)
        for path in paths:
            if not path or _is_steam_install(path):
                continue
            directory = os.path.dirname(path)
            for _ in range(3):
                name = os.path.basename(directory).lower()
                if not directory or directory in ("/", os.path.expanduser("~")) or name in ("drive_c", "windows", "system32", "syswow64"):
                    break
                appid = self._dir_appid(directory)
                if appid:
                    return appid, directory
                directory = os.path.dirname(directory)
        return None
