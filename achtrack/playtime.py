"""Detects running games by inspecting the system's processes - /proc on Linux, libproc on macOS.

A process counts as a game when any of these hold:
  1. it was launched with ACHTRACK_APPID=<appid> in its environment
  2. its executable name matches one configured for a game in the library
  3. it loaded steam_api(64).dll / libsteam_api.so from a folder holding a crack config
     (steam_appid.txt, steam_settings/, steam_emu.ini, OnlineFix.ini, ...)
  4. its Windows .exe sits in (or just below) such a folder
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import re
import struct
import sys
from dataclasses import dataclass

from .scanner import appid_in_dir, crossover_bottle_dirs

STEAM_LIBS = ("steam_api64.dll", "steam_api.dll", "libsteam_api.so")
SYSTEM_PREFIXES = ("/usr/", "/bin/", "/sbin/", "/lib", "/opt/steam", "/snap/", "/nix/", "/app/", "/proc/", "/dev/",
                   "/Applications/", "/System/", "/Library/", "/private/", "/opt/homebrew/", "/opt/local/")
ENV_KEYS = ("ACHTRACK_APPID", "WINEPREFIX", "STEAM_COMPAT_DATA_PATH", "CX_BOTTLE", "CX_BOTTLE_PATH")
DEAD = ("Z", "X", "x")
DETECT_ATTEMPTS = 12  # a game loads steam_api shortly after starting - keep looking for ~1 minute
RECHECK_EVERY = 10    # scans between re-checking that a detected process still looks like the game


@dataclass
class _Proc:
    args: list
    exe: str
    env: dict
    detected: tuple | None = None   # (appid, game_dir)
    reason: str = ""                # why it counted as that game, for `achtrack scan`
    lib_dir: str = ""               # the folder it loaded a Steam library from, if that's why
    checks: int = 0
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


class _LinuxProcesses:
    """Everything the monitor needs to know about a process, read from /proc."""

    @staticmethod
    def available() -> bool:
        return os.path.isdir("/proc/self") and os.path.exists("/proc/self/cmdline")

    def pids(self) -> list:
        try:
            return [e.name for e in os.scandir("/proc") if e.name.isdigit()]
        except OSError:
            return []

    def status(self, pid: str) -> tuple | None:
        """(state, something that changes if the pid is reused) - None if it's gone."""
        stat = _read(f"/proc/{pid}/stat")
        if not stat:
            return None
        state, started = stat_fields(stat)
        return state.decode("ascii", "replace"), started

    def uid(self, pid: str) -> int | None:
        try:
            return os.stat(f"/proc/{pid}").st_uid
        except OSError:
            return None

    def describe(self, pid: str) -> tuple:
        """(arguments, executable, the environment variables we care about)."""
        raw = _read(f"/proc/{pid}/cmdline")
        args = [a.decode("utf-8", "surrogateescape") for a in raw.split(b"\0") if a]
        env = {}
        for item in _read(f"/proc/{pid}/environ").split(b"\0"):
            k, sep, v = item.partition(b"=")
            if sep and k.decode("ascii", "replace") in ENV_KEYS:
                env[k.decode()] = v.decode("utf-8", "surrogateescape")
        return args, _link(f"/proc/{pid}/exe"), env

    def cwd(self, pid: str) -> str:
        return _link(f"/proc/{pid}/cwd")

    def mapped(self, pid: str) -> list:
        """Files mapped into the process - how a loaded steam_api library is spotted."""
        out = []
        for line in _read(f"/proc/{pid}/maps").decode("utf-8", "surrogateescape").splitlines():
            parts = line.split(None, 5)
            if len(parts) == 6:
                out.append(parts[5])
        return out


class _MacProcesses:
    """The same, from macOS's libproc and sysctl. There is no /proc there, but the kernel will
    tell a user everything about their own processes: exact arguments, environment, folder.

    The one thing it won't give up cheaply is the list of mapped files, so a game can't be spotted
    by the Steam library it loaded - only by where its executable lives, which is the common case.
    """
    STATES = {1: "I", 2: "R", 3: "S", 4: "T", 5: "Z"}

    def __init__(self):
        self.libproc = ctypes.CDLL(ctypes.util.find_library("proc"), use_errno=True)
        self.libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)

    @staticmethod
    def available() -> bool:
        return sys.platform == "darwin" and bool(ctypes.util.find_library("proc"))

    def pids(self) -> list:
        count = self.libproc.proc_listallpids(None, 0)
        buf = (ctypes.c_int * (max(count, 0) + 128))()
        count = self.libproc.proc_listallpids(buf, ctypes.sizeof(buf))
        return [str(pid) for pid in buf[:max(count, 0)] if pid > 0]

    def _bsdinfo(self, pid: str) -> bytes | None:
        buf = ctypes.create_string_buffer(136)                 # struct proc_bsdinfo
        return buf.raw if self.libproc.proc_pidinfo(int(pid), 3, 0, buf, 136) == 136 else None

    def status(self, pid: str) -> tuple | None:
        info = self._bsdinfo(pid)
        if info is None:
            return None
        state, = struct.unpack_from("I", info, 4)              # pbi_status
        started = struct.unpack_from("QQ", info, 120)          # pbi_start_tvsec, _tvusec
        return self.STATES.get(state, "?"), started

    def uid(self, pid: str) -> int | None:
        info = self._bsdinfo(pid)
        return struct.unpack_from("I", info, 20)[0] if info else None      # pbi_uid

    def describe(self, pid: str) -> tuple:
        buf = ctypes.create_string_buffer(4096)
        length = self.libproc.proc_pidpath(int(pid), buf, 4096)
        exe = buf.raw[:length].decode("utf-8", "replace") if length > 0 else ""
        mib = (ctypes.c_int * 3)(1, 49, int(pid))              # CTL_KERN, KERN_PROCARGS2
        size = ctypes.c_size_t(0)
        if self.libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0 or not size.value:
            return [], exe, {}
        raw = ctypes.create_string_buffer(size.value)
        if self.libc.sysctl(mib, 3, raw, ctypes.byref(size), None, 0) != 0:
            return [], exe, {}
        data = raw.raw[:size.value]
        argc, = struct.unpack_from("i", data, 0)
        _path, _, rest = data[4:].partition(b"\0")
        parts = rest.lstrip(b"\0").split(b"\0")
        args = [a.decode("utf-8", "surrogateescape") for a in parts[:argc] if a]
        env = {}
        for item in parts[argc:]:
            k, sep, v = item.partition(b"=")
            if sep and k.decode("ascii", "replace") in ENV_KEYS:
                env[k.decode()] = v.decode("utf-8", "surrogateescape")
        return args, exe, env

    def cwd(self, pid: str) -> str:
        buf = ctypes.create_string_buffer(2352)                # struct proc_vnodepathinfo
        if self.libproc.proc_pidinfo(int(pid), 9, 0, buf, 2352) <= 0:
            return ""
        return buf.raw[152:152 + 1024].split(b"\0", 1)[0].decode("utf-8", "replace")

    def mapped(self, pid: str) -> list:
        return []


def process_source():
    for source in (_LinuxProcesses, _MacProcesses):
        if source.available():
            return source()
    return None


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
    if not prefix and proc.env.get("CX_BOTTLE"):               # CrossOver
        for folder in [proc.env.get("CX_BOTTLE_PATH", "")] + [str(d) for d in crossover_bottle_dirs()]:
            candidate = os.path.join(folder, proc.env["CX_BOTTLE"]) if folder else ""
            if candidate and os.path.isdir(os.path.join(candidate, "drive_c")):
                prefix = candidate
                break
    return prefix


def _is_steam_install(path: str) -> bool:
    # Games bought on Steam already get real achievements - leave them alone.
    return "/steamapps/common/" in path.lower()


class ProcessMonitor:
    def __init__(self):
        self.src = process_source()
        self.seen: dict = {}
        self.uid = os.getuid()
        self.self_pid = str(os.getpid())
        self.cache: dict = {}
        self.dir_cache: dict = {}
        self.prefixes: dict = {}   # appid -> Wine prefix of its process, from the last scan

    @staticmethod
    def available() -> bool:
        return _LinuxProcesses.available() or _MacProcesses.available()

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
        if self.src is None:
            return found
        for pid in self.src.pids():
            if pid == self.self_pid:
                continue
            status = self.src.status(pid)
            if status is None:
                continue
            state, started = status
            if state in DEAD:
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
            if appid is not None:
                proc.reason = "ACHTRACK_APPID was set when it started"
            if appid is None and exe_map:
                # A Wine process is the Windows program in its arguments, not the loader that runs
                # it. CrossOver names that loader after the game it launched, and every process in
                # the bottle - services, an installer left open - then carries the game's name.
                windows = any(a.lower().endswith(".exe") for a in proc.args[:4])
                own = [] if windows else [_basename(proc.exe).lower()]
                for name in [_basename(a).lower() for a in proc.args[:2]] + own:
                    if name in exe_map:
                        appid = exe_map[name]
                        proc.reason = f"its name {name!r} is set as this game's executable"
                        break
            if appid is None and proc.detected and proc.lib_dir:
                # Wine and Proton leave helper processes running after a game exits. One that was
                # identified by the Steam library it had loaded is only still that game while the
                # library is still mapped, so don't take the first answer as final.
                proc.checks += 1
                if proc.checks % RECHECK_EVERY == 0 and not self._still_loaded(pid, proc.lib_dir):
                    proc.detected, proc.reason, proc.lib_dir = None, "", ""
                    proc.attempts = DETECT_ATTEMPTS      # it had its chance; don't re-detect it
            if appid is None:
                if proc.detected is None and proc.attempts < DETECT_ATTEMPTS:
                    proc.attempts += 1
                    proc.detected = self._detect(pid, proc)
                if proc.detected:
                    appid, game_dir = proc.detected
            if appid is not None:
                self.seen.setdefault(appid, []).append(
                    (pid, state, " ".join(proc.args[:3]) or proc.exe or "?", proc.reason))
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
        if self.src.uid(pid) != self.uid:
            return _Proc([], "", {}, ignore=True)
        args, exe, env = self.src.describe(pid)
        if not args:
            return _Proc([], "", {}, ignore=True)
        return _Proc(args, exe, env)

    @staticmethod
    def _env_appid(proc: _Proc) -> int | None:
        value = proc.env.get("ACHTRACK_APPID", "").strip()
        return int(value) if value.isdigit() and int(value) > 0 else None

    def _dir_appid(self, directory: str) -> int | None:
        if directory not in self.dir_cache:
            self.dir_cache[directory] = appid_in_dir(directory)
        return self.dir_cache[directory]

    def ignore_matching(self, appid: int) -> int:
        """Stop counting the processes that currently make `appid` look like it's running."""
        pids = {pid for pid, _state, _what, _why in self.seen.get(appid, [])}
        gone = 0
        for key, proc in self.cache.items():
            if key[0] in pids:
                proc.ignore = True
                gone += 1
        self.seen.pop(appid, None)
        return gone

    def _still_loaded(self, pid: str, lib_dir: str) -> bool:
        return any(path.lower().endswith(STEAM_LIBS) and os.path.dirname(path) == lib_dir
                   for path in self.src.mapped(pid))

    def _detect(self, pid: str, proc: _Proc) -> tuple | None:
        # Loaded steam_api library -> its folder carries the crack's config.
        lib_dirs = []
        for path in self.src.mapped(pid):
            if path.lower().endswith(STEAM_LIBS):
                d = os.path.dirname(path)
                if d not in lib_dirs:
                    lib_dirs.append(d)
        for d in lib_dirs:
            if _is_steam_install(d):
                proc.attempts = DETECT_ATTEMPTS
                return None
            appid = self._dir_appid(d)
            if appid:
                proc.reason = f"it loaded a Steam library from {d}, which holds a crack config"
                proc.lib_dir = d
                return appid, d

        # Wine rewrites argv[0] to the Windows path of the .exe.
        prefix = _prefix_of(proc)
        cwd = self.src.cwd(pid)
        exes = [a for a in proc.args[:4] if a.lower().endswith(".exe")]
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
                    proc.reason = f"its Windows executable {path} sits under {directory}"
                    return appid, directory
                directory = os.path.dirname(directory)
        return None
