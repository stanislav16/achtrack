"""Detects running games by inspecting the system's processes - /proc on Linux, libproc on macOS,
the Toolhelp and PSAPI calls on Windows.

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
import glob
import os
import re
import shlex
import struct
import sys
from dataclasses import dataclass

from . import winhost
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
        if not any(a.lower().endswith(".exe") for a in args[:4]):
            target, bottle = crossover_hosted(exe)
            if target:                      # say what it really is, the way a plain Wine process does
                args = [target] + args
                env.setdefault("WINEPREFIX", bottle)
        return args, exe, env

    def cwd(self, pid: str) -> str:
        buf = ctypes.create_string_buffer(2352)                # struct proc_vnodepathinfo
        if self.libproc.proc_pidinfo(int(pid), 9, 0, buf, 2352) <= 0:
            return ""
        return buf.raw[152:152 + 1024].split(b"\0", 1)[0].decode("utf-8", "replace")

    def mapped(self, pid: str) -> list:
        return []


class _WindowsProcesses:
    """The same on Windows, where a game is an ordinary process: its executable is the game's own
    .exe, and the list of modules it has loaded shows a Steam library the way /proc/maps does.

    Only your own session's processes are looked at, and only read: a name, a start time, and the
    paths of the files loaded into it.
    """
    QUERY_LIMITED = 0x1000
    QUERY_AND_READ = 0x0400 | 0x0010
    SNAP_PROCESS = 0x2
    LIST_ALL_MODULES = 0x03

    class _Entry(ctypes.Structure):
        _fields_ = [("dwSize", ctypes.c_uint32), ("cntUsage", ctypes.c_uint32), ("th32ProcessID", ctypes.c_uint32),
                    ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", ctypes.c_uint32),
                    ("cntThreads", ctypes.c_uint32), ("th32ParentProcessID", ctypes.c_uint32),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", ctypes.c_uint32),
                    ("szExeFile", ctypes.c_wchar * 260)]

    def __init__(self):
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.psapi = ctypes.WinDLL("psapi", use_last_error=True)
        handle, dword, pointer = ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER
        self.k32.CreateToolhelp32Snapshot.restype = handle
        self.k32.CreateToolhelp32Snapshot.argtypes = [dword, dword]
        self.k32.Process32FirstW.argtypes = self.k32.Process32NextW.argtypes = [handle, pointer(self._Entry)]
        self.k32.OpenProcess.restype = handle
        self.k32.OpenProcess.argtypes = [dword, ctypes.c_int, dword]
        self.k32.CloseHandle.argtypes = [handle]
        self.k32.QueryFullProcessImageNameW.argtypes = [handle, dword, ctypes.c_wchar_p, pointer(dword)]
        self.k32.GetProcessTimes.argtypes = [handle] + [pointer(ctypes.c_uint64)] * 4
        self.k32.GetExitCodeProcess.argtypes = [handle, pointer(dword)]
        self.k32.ProcessIdToSessionId.argtypes = [dword, pointer(dword)]
        self.psapi.EnumProcessModulesEx.argtypes = [handle, pointer(handle), dword, pointer(dword), dword]
        self.psapi.GetModuleFileNameExW.argtypes = [handle, handle, ctypes.c_wchar_p, dword]
        self.session = self._session(os.getpid())

    @staticmethod
    def available() -> bool:
        return os.name == "nt"

    def _session(self, pid: int) -> int | None:
        out = ctypes.c_uint32()
        return out.value if self.k32.ProcessIdToSessionId(pid, ctypes.byref(out)) else None

    def _open(self, pid: str, access: int):
        return self.k32.OpenProcess(access, 0, int(pid)) or None

    def pids(self) -> list:
        snapshot = self.k32.CreateToolhelp32Snapshot(self.SNAP_PROCESS, 0)
        if not snapshot or snapshot == ctypes.c_void_p(-1).value:
            return []
        out, entry = [], self._Entry()
        entry.dwSize = ctypes.sizeof(self._Entry)
        try:
            more = self.k32.Process32FirstW(snapshot, ctypes.byref(entry))
            while more:
                if entry.th32ProcessID > 4:                # 0 and 4 are the kernel's own
                    out.append(str(entry.th32ProcessID))
                more = self.k32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            self.k32.CloseHandle(snapshot)
        return out

    def status(self, pid: str) -> tuple | None:
        handle = self._open(pid, self.QUERY_LIMITED)
        if handle is None:
            return None
        try:
            created, exited, kernel, user = (ctypes.c_uint64() for _ in range(4))
            if not self.k32.GetProcessTimes(handle, *(ctypes.byref(v) for v in (created, exited, kernel, user))):
                return None
            code = ctypes.c_uint32()
            running = self.k32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value == 259   # STILL_ACTIVE
            return ("R" if running else "Z"), created.value
        finally:
            self.k32.CloseHandle(handle)

    def uid(self, pid: str) -> int | None:
        """Not a user id here: the id of the login session, which is what "mine" means on Windows."""
        return self._session(int(pid))

    def own_uid(self) -> int | None:
        return self.session

    def describe(self, pid: str) -> tuple:
        handle = self._open(pid, self.QUERY_LIMITED)
        if handle is None:
            return [], "", {}
        try:
            buf = ctypes.create_unicode_buffer(32768)
            size = ctypes.c_uint32(len(buf))
            if not self.k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return [], "", {}
            exe = buf.value
        finally:
            self.k32.CloseHandle(handle)
        # Another process's arguments and environment aren't offered the way its path is, and the
        # path is the part that identifies a game.
        return [exe], exe, {}

    def cwd(self, pid: str) -> str:
        return ""

    def mapped(self, pid: str) -> list:
        handle = self._open(pid, self.QUERY_AND_READ)
        if handle is None:
            return []
        try:
            needed = ctypes.c_uint32()
            modules = (ctypes.c_void_p * 1024)()
            if not self.psapi.EnumProcessModulesEx(handle, modules, ctypes.sizeof(modules), ctypes.byref(needed),
                                                   self.LIST_ALL_MODULES):
                return []
            count = min(needed.value // ctypes.sizeof(ctypes.c_void_p), len(modules))
            out, buf = [], ctypes.create_unicode_buffer(32768)
            for module in modules[:count]:
                if self.psapi.GetModuleFileNameExW(handle, module, buf, len(buf)):
                    out.append(buf.value)
            return out
        finally:
            self.k32.CloseHandle(handle)


def process_source():
    for source in (_LinuxProcesses, _MacProcesses, _WindowsProcesses):
        if source.available():
            return source()
    return None


def _basename(arg: str) -> str:
    return re.split(r"[\\/]", arg)[-1]


def win_to_linux(arg: str, cwd: str, prefix: str) -> str | None:
    if winhost.IS_WINDOWS:              # already a real path: there is nothing to translate
        if os.path.isabs(arg):
            return arg
        return os.path.join(cwd, arg) if cwd else None
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


EXE_IN_LNK = re.compile(rb"[A-Za-z]:\\[\x20-\x7e]{2,240}?\.exe", re.I)


def shortcut_target(data: bytes) -> str:
    """The program a Windows .lnk shortcut starts, as a Windows path - "" if it can't be found."""
    found = [m.decode("ascii") for m in EXE_IN_LNK.findall(data)]
    wide = data.decode("utf-16-le", "ignore")
    found += re.findall(r"[A-Za-z]:\\[^\x00-\x1f\"<>|]{2,240}?\.exe", wide, re.I)
    return found[0] if found else ""


def crossover_hosted(exe: str) -> tuple:
    """(Windows program, bottle) for a game started from one of CrossOver's launcher apps.

    CrossOver makes a small .app per installed program and runs the game *as* that app, so the
    dock shows the game's own name. The process then says nothing about what it is - its only
    argument is the launcher. But the launcher's Info.plist names a script, the script names a
    shortcut in the bottle, and the shortcut names the program.
    """
    cut = exe.find(".app/Contents/MacOS/")
    if cut < 0:
        return "", ""
    try:
        import plistlib
        with open(exe[:cut] + ".app/Contents/Info.plist", "rb") as f:
            command = plistlib.load(f).get("CrossOverHelperCommand") or ""
    except (OSError, ValueError):
        return "", ""
    try:                                    # it is a command line, with the path in quotes
        command = shlex.split(command)[0]
    except (ValueError, IndexError):
        return "", ""
    where = command.find("/desktopdata/")
    if where < 0:
        return "", ""
    bottle = command[:where]
    data = _read(command)
    target = ""
    if data[:2] == b"#!":                                   # a script: wine --start "C:/…/Game.lnk"
        text = data.decode("utf-8", "replace")
        named = re.findall(r'"([A-Za-z]:[/\\][^"]+\.(?:lnk|exe))"', text, re.I)
        for path in named:
            if path.lower().endswith(".exe"):
                target = path
            else:
                real = win_to_linux(path, "", bottle)
                target = shortcut_target(_read(real)) if real else ""
            if target:
                break
    else:
        target = shortcut_target(data)
    return target, bottle if target else ""


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
    return "/steamapps/common/" in path.lower().replace("\\", "/")


class ProcessMonitor:
    def __init__(self):
        self.src = process_source()
        self.seen: dict = {}
        own = getattr(self.src, "own_uid", None)
        self.uid = own() if own else os.getuid()
        self.self_pid = str(os.getpid())
        self.cache: dict = {}
        self.dir_cache: dict = {}
        self.prefixes: dict = {}   # appid -> Wine prefix of its process, from the last scan

    @staticmethod
    def available() -> bool:
        return _LinuxProcesses.available() or _MacProcesses.available() or _WindowsProcesses.available()

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

    def _library_dirs(self, directory: str) -> list:
        """Where a game engine keeps the Steam library when it isn't beside the executable - and
        a crack keeps its config beside the library. Unreal puts it deep under Engine/, Unity
        under <Game>_Data/Plugins."""
        key = ("libs", directory)
        if key not in self.dir_cache:
            found = []
            for pattern in (("Engine", "Binaries", "ThirdParty", "Steamworks", "*", "Win64"),
                            ("Engine", "Binaries", "ThirdParty", "Steamworks", "*"),
                            ("*_Data", "Plugins", "x86_64"), ("*_Data", "Plugins")):
                found += [d for d in glob.glob(os.path.join(glob.escape(directory), *pattern)) if os.path.isdir(d)]
            self.dir_cache[key] = found
        return self.dir_cache[key]

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
        return any(path.lower().endswith(STEAM_LIBS)
                   and os.path.normcase(os.path.dirname(path)) == os.path.normcase(lib_dir)
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
        if winhost.IS_WINDOWS:
            paths = [p for p in paths if p and not winhost.is_system_path(p)]
        for path in paths:
            if not path or _is_steam_install(path):
                continue
            directory = os.path.dirname(path)
            for _ in range(4):
                name = os.path.basename(directory).lower()
                if (not directory or directory in ("/", os.path.expanduser("~")) or os.path.dirname(directory) == directory
                        or name in ("drive_c", "windows", "system32", "syswow64", "program files",
                                    "program files (x86)")):
                    break
                appid = self._dir_appid(directory)
                if appid:
                    proc.reason = f"its Windows executable {path} sits under {directory}"
                    return appid, directory
                for beside in self._library_dirs(directory):
                    appid = self._dir_appid(beside)
                    if appid:
                        proc.reason = (f"its Windows executable {path} belongs to the game in {directory}, "
                                       f"whose crack config is in {beside}")
                        return appid, beside
                directory = os.path.dirname(directory)
        return None
