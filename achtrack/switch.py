"""Nintendo Switch games played in Ryubing (the community fork of Ryujinx).

A Switch game has no achievements for AchTrack to read: Nintendo never made any. What it does have
is play reports, the statistics a game sends Nintendo, and Ryubing writes those to its log:

    PlayReport log:
     ...
     Room: user_data
     Report: {
        "TotalRaceNum": 12,
        "GrandPrixTrophy": "3000...",
        ...
    }

So the achievements here are AchTrack's own, worked out from those numbers. Each report holds the
game's running totals, which makes the newest one the whole story: a report AchTrack missed loses
nothing, and Ryubing's habit of deleting old logs doesn't either.

Which game is running can't come from the log, because nothing is written when a game closes, or
from the emulator's name, because it stays open between games. A running game holds the .lock file
in its save folder open, so that is what says it is being played.
"""
from __future__ import annotations

import json
import os
import re
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import _xdg
from .emulators import AchState

EMULATOR = "ryubing"
PROCESSES = ("ryujinx",)    # the program is still called this
PLATFORM = "switch"


def data_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Ryujinx"
    return _xdg("XDG_CONFIG_HOME", ".config") / "Ryujinx"


def logs_dirs() -> tuple:
    return (Path.home() / "Library" / "Logs" / "Ryujinx", data_dir() / "Logs")


def app() -> Path | None:
    """The emulator itself, on a Mac."""
    for folder in (Path("/Applications"), Path.home() / "Applications"):
        if (folder / "Ryujinx.app").is_dir():
            return folder / "Ryujinx.app"
    return None


def log_files() -> list:
    found: list = []
    for folder in logs_dirs():
        try:
            found += [str(p) for p in folder.glob("Ryujinx_*.log") if p.is_file()]
        except OSError:
            pass
    return sorted(found)


def is_log(path: str) -> bool:
    return os.path.dirname(path) in {str(d) for d in logs_dirs()}


def save_id(title_id: str) -> str | None:
    """The folder under bis/user/save holding a game's save. Those folders are numbered in the order
    games first ran, and the system's own save index says which is which."""
    try:
        data = (data_dir() / "bis" / "system" / "save" / "8000000000000000" / "0" / "imkvdb.arc").read_bytes()
    except OSError:
        return None
    if data[:4] != b"IMKV":
        return None
    wanted = int(title_id, 16)
    offset = 12
    while offset + 12 <= len(data) and data[offset:offset + 4] == b"IMEN":
        key_size, value_size = struct.unpack_from("<II", data, offset + 4)
        key = data[offset + 12:offset + 12 + key_size]
        value = data[offset + 12 + key_size:offset + 12 + key_size + value_size]
        offset += 12 + key_size + value_size
        if len(key) >= 33 and len(value) >= 8 and struct.unpack_from("<Q", key)[0] == wanted and key[32] == 1:
            return f"{struct.unpack_from('<Q', value)[0]:016x}"     # type 1: a user's save
    return None


def game_dirs() -> list:
    """The folders Ryubing is told to look for games in."""
    try:
        with open(data_dir() / "Config.json", encoding="utf-8") as f:
            return [d for d in json.load(f).get("game_dirs") or [] if isinstance(d, str)]
    except (OSError, ValueError):
        return []


# --- what a report says -----------------------------------------------------------------------------

def _count(value) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def _bits(value) -> int:
    """How many flags are set in a report's "0x…" bit field."""
    if isinstance(value, dict):
        value = value.get("Value")
    try:
        return bin(int(str(value), 16)).count("1")
    except ValueError:
        return 0


def _field(name: str):
    return lambda report: _count(report.get(name))


def _flags(name: str):
    return lambda report: _bits(report.get(name))


def _trophies(report: dict) -> int:
    # One digit for each cup in each class, 0 until a trophy is won there.
    return sum(digit != "0" for digit in str(report.get("GrandPrixTrophy") or ""))


def _parts(report: dict) -> int:
    return sum(_bits(report.get(name)) for name in ("OpenBodyFlags", "OpenTireFlags", "OpenWingFlags"))


def _battle_modes(report: dict) -> int:
    return sum(_count(report.get(f"Total{mode}BattleNum")) > 0
               for mode in ("Balloon", "Keidoro", "Bomb", "Coin", "Shine"))


@dataclass(frozen=True)
class Goal:
    name: str
    title: str
    detail: str
    measure: object         # report -> a number
    target: int
    counted: bool = True    # whether the number is worth showing as progress


MINUTE_OF_FRAMES = 60 * 60

MARIO_KART_GOALS = (
    Goal("race_1", "Lights Out", "Finish your first race.", _field("TotalRaceNum"), 1, False),
    Goal("race_50", "Weekend Racer", "Finish 50 races.", _field("TotalRaceNum"), 50),
    Goal("race_250", "Season Ticket", "Finish 250 races.", _field("TotalRaceNum"), 250),
    Goal("race_1000", "Lifer", "Finish 1,000 races.", _field("TotalRaceNum"), 1000),
    Goal("trophy_1", "On the Podium", "Win your first Grand Prix trophy.", _trophies, 1, False),
    Goal("trophy_12", "Twelve Cups", "Win 12 Grand Prix trophies.", _trophies, 12),
    Goal("trophy_30", "Halfway There", "Win 30 Grand Prix trophies.", _trophies, 30),
    Goal("trophy_60", "Full Cabinet", "Win a trophy in all 60 Grand Prix, every cup in every class.", _trophies, 60),
    Goal("ending", "Roll the Credits", "Watch the ending.", _flags("WatchEndingFlags"), 1, False),
    Goal("gold_mario", "Solid Gold", "Unlock Gold Mario.", _field("OpenGoldMario"), 1, False),
    Goal("ghost_150", "Ghostbuster", "Beat a Staff Ghost in 150cc Time Trials.",
         _flags("Win150ccStaffGhostFlags"), 1, False),
    Goal("ghost_150_all", "No More Ghosts", "Beat the Staff Ghost on all 48 tracks in 150cc.",
         _flags("Win150ccStaffGhostFlags"), 48),
    Goal("ghost_200", "Faster Than Staff", "Beat a Staff Ghost in 200cc Time Trials.",
         _flags("Win200ccStaffGhostFlags"), 1, False),
    Goal("ghost_200_all", "Staff Meeting", "Beat the Staff Ghost on all 48 tracks in 200cc.",
         _flags("Win200ccStaffGhostFlags"), 48),
    Goal("coin_1000", "Pocket Change", "Collect 1,000 coins.", _field("TotalCoin"), 1000),
    Goal("coin_10000", "Coin Hoarder", "Collect 10,000 coins.", _field("TotalCoin"), 10000),
    Goal("parts_30", "Spare Parts", "Own 30 karts, tires and gliders.", _parts, 30),
    Goal("parts_50", "Well Equipped", "Own 50 karts, tires and gliders.", _parts, 50),
    Goal("mii_suit", "Dressed for It", "Unlock a racing suit for your Mii.", _flags("OpenMiiSuitsFlags"), 1, False),
    Goal("ultra_100", "Purple Sparks", "Land 100 Ultra Mini-Turbos.", _field("TotalMiniTurbo3"), 100),
    Goal("drift_5000", "Sideways", "Drift 5,000 times.", _field("TotalDrift"), 5000),
    Goal("trick_1000", "Showing Off", "Do 1,000 jump tricks.", _field("TotalJumpAction"), 1000),
    Goal("lakitu_50", "On First-Name Terms", "Get rescued by Lakitu 50 times.", _field("TotalJugemRescue"), 50),
    Goal("fly_30", "Frequent Flyer", "Spend 30 minutes gliding.", _field("TotalFlyTimeFrame"), 30 * MINUTE_OF_FRAMES,
         False),
    Goal("swim_30", "Deep End", "Spend 30 minutes underwater.", _field("TotalSwimTimeFrame"), 30 * MINUTE_OF_FRAMES,
         False),
    Goal("battle_1", "Picking a Fight", "Play a Battle match.", _battle_modes, 1, False),
    Goal("battle_all", "All-Rounder", "Play all five Battle modes.", _battle_modes, 5),
)


@dataclass(frozen=True)
class Title:
    key: str                # remembered in the library as "ports"[<key>], the way a native port is
    name: str
    title_id: str
    genre: str
    art: str
    goals: tuple

    def save_folder(self) -> Path | None:
        """This game's save, if it has run in Ryubing on this machine."""
        number = save_id(self.title_id)
        return data_dir() / "bis" / "user" / "save" / number if number else None

    # What ports.Port offers, so saves are backed up and restored the same way.
    def saves_dir(self) -> Path | None:
        return self.save_folder()

    def schema(self) -> list:
        return [{"name": goal.name, "display_name": goal.title, "description": goal.detail, "icon": "",
                 "icon_gray": "", "hidden": 0, "global_pct": None, "kind": None}
                for goal in self.goals]

    def states(self, report: dict, when: int) -> dict:
        out = {}
        for goal in self.goals:
            value = goal.measure(report)
            done = value >= goal.target
            out[goal.name] = AchState(goal.name, done, when if done else None,
                                      float(min(value, goal.target)) if goal.counted else None,
                                      float(goal.target) if goal.counted else None)
        return out

    def game_file(self) -> str:
        """The game in Ryubing's folders, found by the id in its name."""
        for folder in game_dirs():
            try:
                for entry in os.scandir(folder):
                    if self.title_id.lower() in entry.name.lower() and entry.name.lower().endswith((".nsp", ".xci")):
                        return entry.path
            except OSError:
                continue
        return ""


MARIO_KART = Title(
    key="mk8d",
    name="Mario Kart 8 Deluxe",
    title_id="0100152000022000",
    genre="Racing",
    art="https://assets.nintendo.com/image/upload/ar_16:9,c_lpad,w_1240/b_white/f_auto/q_auto/store/software/switch/"
        "70010000000153/de697f487a36d802dd9a5ff0341f717c8486221f2f1219b675af37aca63bc453",
    goals=MARIO_KART_GOALS,
)
TITLES = (MARIO_KART,)


def title_of(db, appid: int) -> Title | None:
    """The Switch game behind this library entry, if any."""
    linked = db.get_meta("ports") or {}
    return next((title for title in TITLES if linked.get(title.key) == appid), None)


# --- reading the log --------------------------------------------------------------------------------

_LOADED = re.compile(r"Application Loaded: .*\[([0-9A-Fa-f]{16})\]")


class _LogState:
    """How far one log has been read. Logs grow while the game runs and only the new part is read."""

    def __init__(self, inode: int):
        self.inode = inode
        self.offset = 0
        self.title = ""             # the game loaded at the point reached
        self.block: list = []       # the report being read, when a read stopped inside one
        self.in_report = False
        self.room = ""
        self.latest: dict = {}      # title id -> its newest report


_read: dict = {}


def reports_in(path: str) -> dict:
    """{title id: its newest report} for one log."""
    st = os.stat(path)
    state = _read.get(path)
    if state is None or state.inode != st.st_ino or st.st_size < state.offset:
        state = _read[path] = _LogState(st.st_ino)
    with open(path, "rb") as f:
        f.seek(state.offset)
        chunk = f.read()
    end = chunk.rfind(b"\n") + 1            # a half-written last line waits for the next read
    state.offset += end
    for line in chunk[:end].decode("utf-8", "replace").splitlines():
        if state.in_report:
            state.block.append(line)
            if line == "}":
                state.in_report = False
                try:
                    report = json.loads("\n".join(state.block))
                except ValueError:
                    continue
                if state.room == "user_data" and state.title:
                    state.latest[state.title] = report
            continue
        if line.startswith(" Room:"):
            state.room = line.split(":", 1)[1].strip(" \0")      # Ryubing leaves a NUL on the end
        elif line.startswith(" Report: {"):
            state.in_report, state.block = True, ["{"]
        else:
            loaded = _LOADED.search(line)
            if loaded:
                state.title = loaded.group(1).lower()
    return state.latest


def forget(path: str) -> None:
    _read.pop(path, None)


def parse_log(path: Path, title: Title = MARIO_KART) -> dict:
    """{achievement: AchState} from one log - the same shape the emulator parsers give."""
    report = reports_in(str(path)).get(title.title_id.lower())
    if report is None:
        return {}
    # A report says what, not when, and the log's clock restarts with each game. The log's last write
    # is the closest thing to when.
    return title.states(report, int(os.stat(path).st_mtime))
