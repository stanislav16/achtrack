"""Native PC ports of PlayStation games, and the trophies they report.

A port runs the console game's own code, so the game still unlocks trophies the console way - and
the port, having no PlayStation account behind it, writes each one to its output instead:

    Runtime: trophy 12 unlocked

The number is the trophy's id in the game's PlayStation trophy list, so that list (fetched from
PlayStation, the same way PSN sync fetches yours) says which trophy it is. The output reaches a
file when the port is told to keep a log; AchTrack reads those files the way it reads an
emulator's achievement file.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import _xdg


@dataclass(frozen=True)
class Port:
    key: str                 # remembered in the library as "port:<key>"
    name: str                # the game, as PlayStation names it
    title_ids: tuple         # its store ids, to find its trophy list
    process: str             # the program that runs while the game is played
    data_dirs: tuple         # where the port keeps its files
    trophy_line: re.Pattern = field(compare=False)

    def data_dir(self) -> Path | None:
        return next((d for d in self.data_dirs if d.is_dir()), None)

    def log_files(self) -> list:
        base = self.data_dir()
        if base is None:
            return []
        try:
            return sorted(str(p) for p in (base / "logs").glob("*.log") if p.is_file())
        except OSError:
            return []


BLOODBORNE = Port(
    key="bbport",
    name="Bloodborne",
    # The Game of the Year edition first: its list is the one the port's game files come from.
    title_ids=("CUSA03173", "CUSA00207", "CUSA00900", "CUSA01363", "CUSA03023"),
    process="bb-probe",
    data_dirs=(_xdg("XDG_DATA_HOME", ".local/share") / "bbport",),
    trophy_line=re.compile(r"Runtime: trophy (\d+) unlocked"),
)

PORTS = (BLOODBORNE,)
EMULATOR = "port"
KEEP_LOGS = 3           # the newest few stay, for looking into a crash; older ones go once read
QUIET_FOR = 600         # seconds a log must have gone unwritten before it counts as finished


def port_for_log(path: str) -> Port | None:
    for port in PORTS:
        base = port.data_dir()
        if base is not None and os.path.dirname(os.path.dirname(path)) == str(base):
            return port
    return None


# A log holds everything the game prints and can grow large, and it changes constantly while the
# game runs - so each file is read once, then only what was added since.
_read: dict = {}        # path -> (inode, bytes read, trophy ids seen)


def trophies_in(path: str, port: Port) -> set:
    st = os.stat(path)
    inode, offset, ids = _read.get(path, (st.st_ino, 0, set()))
    if inode != st.st_ino or st.st_size < offset:       # replaced or cut short: start again
        offset, ids = 0, set()
    ids = set(ids)
    with open(path, "rb") as f:
        f.seek(offset)
        chunk = f.read()
    end = chunk.rfind(b"\n") + 1                         # a half-written last line waits for the next read
    for match in port.trophy_line.finditer(chunk[:end].decode("utf-8", "replace")):
        ids.add(int(match.group(1)))
    _read[path] = (st.st_ino, offset + end, ids)
    return ids


def tidy_logs(port: Port, read: set, running: bool) -> list:
    """Deletes old logs whose trophies are already in the library. Returns what went.

    `read` holds the logs AchTrack has finished reading. A log is only removed when it is not one
    of the newest KEEP_LOGS, nobody has written to it for a while, and the game isn't running -
    so a session AchTrack hasn't read yet always survives until it has been.
    """
    if running:
        return []
    gone = []
    now = time.time()
    for path in port.log_files()[:-KEEP_LOGS]:          # names start with the date: oldest first
        try:
            quiet = now - os.stat(path).st_mtime > QUIET_FOR
        except OSError:
            continue
        if path in read and quiet:
            try:
                os.remove(path)
            except OSError:
                continue
            _read.pop(path, None)
            gone.append(path)
    return gone


def parse_log(path: Path) -> dict:
    """{trophy name: AchState} for one port log - the same shape the emulator parsers give."""
    from .emulators import AchState
    port = port_for_log(str(path)) or BLOODBORNE
    ids = trophies_in(str(path), port)
    # The log says what, not when. Its last write is the closest thing to when: for a log read
    # live that is now, for one from a session AchTrack missed it is when that session ended.
    when = int(os.stat(path).st_mtime)
    return {f"trophy_{i}": AchState(f"trophy_{i}", True, when) for i in sorted(ids)}
