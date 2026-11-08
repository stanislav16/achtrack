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

Mario Kart World is a Switch 2 game, which Ryubing can't run. It runs natively on a Mac instead, as an
app built with kartnative, and that app writes each play report the game saves to playreports.jsonl.
Those reports are about one race or cup each rather than totals, so its numbers come from adding up
the whole file.

Mario Kart 8 Deluxe can run either way. Its native app (SwitchNative) writes the same user_data reports
Ryubing logs, running totals each time, so the newest one is the whole story there too, and both feed
one library entry.
"""
from __future__ import annotations

import json
import os
import re
import signal
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import formats
from .config import ASSET_DIR, _xdg
from .emulators import AchState

EMULATOR = "ryubing"
NATIVE_EMULATOR = "nxnative"
PROCESSES = ("ryujinx",)    # the program is still called this
PLATFORM = "switch"


def data_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Ryujinx"
    return _xdg("XDG_CONFIG_HOME", ".config") / "Ryujinx"


def logs_dirs() -> tuple:
    return (Path.home() / "Library" / "Logs" / "Ryujinx", data_dir() / "Logs")


def app_folders() -> tuple:
    return (Path("/Applications"), Path.home() / "Applications")


def app() -> Path | None:
    """The emulator itself, on a Mac."""
    for folder in app_folders():
        if (folder / "Ryujinx.app").is_dir():
            return folder / "Ryujinx.app"
    return None


def emulator_pids() -> list:
    """Ryubing, if it is running."""
    try:
        done = subprocess.run(["pgrep", "-x", "Ryujinx"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(pid) for pid in done.stdout.split()]


def emulator_idle() -> bool:
    """Whether a running Ryubing has no game open. It reloads its settings each time one stops."""
    logs = log_files()
    if not logs:
        return True
    try:
        data = Path(max(logs, key=os.path.getmtime)).read_bytes()
    except OSError:
        return False
    loaded = data.rfind(b"Application Loaded:")
    return loaded < 0 or loaded < data.rfind(b"Application ReloadConfig:")


def close_emulator() -> None:
    """Closes an idle Ryubing, so that it can be started with a game. Raises OSError if it can't."""
    pids = emulator_pids()
    if pids and not emulator_idle():
        raise OSError("Ryubing has something open. Close it there first, then press Play again.")
    for pid in pids:
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 5
    while emulator_pids():
        if time.monotonic() > deadline:
            raise OSError("Ryubing didn't close. Quit it yourself, then press Play again.")
        time.sleep(0.1)


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
    from_save: bool = False  # measured from the game's save rather than its reports (Mario Kart World's collectables)


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


class _Totals(dict):
    """A game whose every report holds its running totals (Mario Kart 8 Deluxe): the newest report is the tally."""

    def add(self, event: str, report: dict) -> None:
        self.clear()
        self.update(report)


# --- Mario Kart World: its reports added up ---------------------------------------------------------

# Race_FirstCircuit_A_00 is a course, and InterStageEx1_FirstCircuitToWestCity_B_00 is the drive that ends on West City.
_WORLD_COURSE = re.compile(r"(?:Race_|InterStageEx\d+_.+?To)([A-Za-z]+)_")


class _WorldTally:
    """What Mario Kart World's reports add up to so far. A report covers one race, so nothing here is a
    running total until they have all been counted."""

    def __init__(self):
        self.events: dict = {}
        self.races = self.wins = self.podiums = self.coins = self.best_coins = self.overtakes = self.comebacks = 0
        self.cups = self.perfect_cups = 0
        self.gold_cups: set = set()
        self.courses: set = set()
        self.drivers: set = set()
        self.vehicles: set = set()
        self.last_rank = None

    def add(self, event: str, report: dict) -> None:
        self.events[event] = self.events.get(event, 0) + 1
        if event == "game_repo_race_finalize":
            self.last_rank = None
            if report.get("IsRetire"):
                return
            rank = _count(report.get("Rank"))       # counted from 0: 2 is third place
            coins = _count(report.get("CoinNum"))
            self.last_rank = rank
            self.races += 1
            self.wins += rank == 0
            self.podiums += rank <= 2
            self.coins += coins
            self.best_coins = max(self.best_coins, coins)
            course = _WORLD_COURSE.match(str(report.get("CourseName") or ""))
            if course:
                self.courses.add(course.group(1))
            for field, seen in (("DriverId", self.drivers), ("VehicleId", self.vehicles)):
                if report.get(field) is not None:
                    seen.add(report[field])
        elif event == "game_repo_grandprix_finalize":
            self.cups += 1
            if _count(report.get("MaxRank")) == 1:          # counted from 1, unlike a race's
                self.gold_cups.add(report.get("CupId"))
            if _count(report.get("MaxPoint")) >= 60:        # 15 for each of the four races
                self.perfect_cups += 1
        elif event == "game_repo_rank_change_in_race":      # written just after its race
            changes = [c for c in report.get("RankChangeInfos") or [] if isinstance(c, dict)]
            self.overtakes += sum(_count(c.get("AfterRank")) < _count(c.get("BeforeRank")) for c in changes)
            if self.last_rank == 0 and changes and _count(changes[0].get("BeforeRank")) == 24:
                self.comebacks += 1


# --- Mario Kart World: the Free Roam collectables in its save ---------------------------------------
#
# The reports say nothing about medallions, ? Panels or P-Switches, but the save (save/Play/account.sav) does. It is the
# game's GameData format: after a 0x28-byte header, (name hash, value) pairs up to the offset at byte 8, in runs by type,
# each run opened by a pair with hash 0 and the type number as its value. An array's value is the file offset of a u32
# count followed by the elements. The three lists below were told apart with the game's own Free Roam map, whose counters
# follow them: each was filled with a different number of entries in a test save and the map showed those numbers.

_UINT_ARRAY, _UINT64_ARRAY = 21, 25
_MEDALLION_IDS = 0xECFAC610     # UInt64Array[1000]: the placement id of each Peach Medallion collected (with a date beside)
_PANEL_IDS = 0xB2DCE78C         # UInt64Array[1000]: each ? Panel hit
_MISSION_IDS = 0xEF78D378       # UIntArray[3000]: each P-Switch mission tried, by the hash of its name
_MISSION_CLEARS = 0xD99C4331    # UIntArray[3000], beside it: times cleared (the map counts the missions above 0)
MEDALLIONS, PANELS, P_SWITCHES = 200, 150, 394   # the game's Free Roam collection lists (FreerunCollectionListGParam)


class _WorldSave:
    def __init__(self, medallions: int = 0, panels: int = 0, p_switches: int = 0):
        self.medallions, self.panels, self.p_switches = medallions, panels, p_switches


class _GameData:
    """A save in the GameData format of Nintendo's recent games (Mario Kart World, Donkey Kong Bananza): after a 0x28-byte
    header, (name hash, value) pairs up to the offset at byte 8, in runs by type, each run opened by a pair with hash 0 and
    the type number as its value. A scalar's value is the pair's own word; an array's is the file offset of a u32 count
    followed by the elements."""

    def __init__(self, path: Path):
        self.data = Path(path).read_bytes()
        if len(self.data) < 0x28 or self.data[:4] != b"\x04\x03\x02\x01":
            raise ValueError("not a GameData save")
        end = min(struct.unpack_from("<I", self.data, 8)[0], len(self.data))
        self.found: dict = {}
        kind = 0
        for pos in range(0x28, end - 7, 8):
            name, value = struct.unpack_from("<II", self.data, pos)
            if name == 0:
                kind = value
            else:
                self.found[name] = (kind, value)

    def array(self, name: int, want: int, fmt: str = "I") -> list:
        kind, at = self.found.get(name, (None, 0))
        if kind != want or at + 4 > len(self.data):
            raise ValueError(f"no list {name:#x} in the save")
        count = struct.unpack_from("<I", self.data, at)[0]
        layout = "<%d%s" % (count, fmt)
        if at + 4 + struct.calcsize(layout) > len(self.data):
            raise ValueError(f"list {name:#x} runs past the end of the save")
        return list(struct.unpack_from(layout, self.data, at + 4))

    def flags_set(self, names) -> int:
        """How many of these Bool entries are true (ones the save doesn't hold count as false)."""
        return sum(1 for name in names if self.found.get(name) == (0, 1))


def read_world_save(path: Path) -> _WorldSave:
    """Counts the collectables in a Mario Kart World save. Raises ValueError if it isn't one."""
    save = _GameData(path)

    def array(name: int, want: int) -> list:
        return save.array(name, want, "Q" if want == _UINT64_ARRAY else "I")

    clears = array(_MISSION_CLEARS, _UINT_ARRAY)
    missions = {m for m, n in zip(array(_MISSION_IDS, _UINT_ARRAY), clears) if m and n > 0}
    return _WorldSave(len({i for i in array(_MEDALLION_IDS, _UINT64_ARRAY) if i}),
                      len({i for i in array(_PANEL_IDS, _UINT64_ARRAY) if i}), len(missions))


def _tallied(name: str):
    return lambda tally: getattr(tally, name)


def _seen(name: str):
    return lambda tally: len(getattr(tally, name))


def _times(event: str):
    return lambda tally: tally.events.get(event, 0)


MARIO_KART_WORLD_GOALS = (
    Goal("race_1", "Green Light", "Finish your first race.", _tallied("races"), 1, False),
    Goal("race_25", "Regular", "Finish 25 races.", _tallied("races"), 25),
    Goal("race_100", "Road Warrior", "Finish 100 races.", _tallied("races"), 100),
    Goal("race_500", "Long Haul", "Finish 500 races.", _tallied("races"), 500),
    Goal("win_1", "First Across the Line", "Win a race.", _tallied("wins"), 1, False),
    Goal("win_10", "Double Digits", "Win 10 races.", _tallied("wins"), 10),
    Goal("win_50", "Hard to Catch", "Win 50 races.", _tallied("wins"), 50),
    Goal("win_200", "Untouchable", "Win 200 races.", _tallied("wins"), 200),
    Goal("podium_25", "Podium Regular", "Finish in the top three 25 times.", _tallied("podiums"), 25),
    Goal("comeback", "Last to First", "Start a race in 24th and win it.", _tallied("comebacks"), 1, False),
    Goal("overtake_500", "Coming Through", "Overtake 500 racers.", _tallied("overtakes"), 500),
    Goal("overtake_5000", "Nobody's Safe", "Overtake 5,000 racers.", _tallied("overtakes"), 5000),
    Goal("cup_1", "Cup Runner", "Finish a Grand Prix cup.", _tallied("cups"), 1, False),
    Goal("cup_25", "Grand Tourer", "Finish 25 Grand Prix cups.", _tallied("cups"), 25),
    Goal("gold_1", "Gold Standard", "Finish first in a Grand Prix cup.", _seen("gold_cups"), 1, False),
    Goal("gold_8", "Clean Sweep", "Finish first in all 8 Grand Prix cups.", _seen("gold_cups"), 8),
    Goal("perfect", "Perfect Score", "Win all four races of a Grand Prix cup.", _tallied("perfect_cups"), 1, False),
    Goal("course_5", "Sightseer", "Race on 5 different courses.", _seen("courses"), 5),
    Goal("course_15", "Road Tripper", "Race on 15 different courses.", _seen("courses"), 15),
    Goal("course_30", "The Whole World", "Race on all 30 courses.", _seen("courses"), 30),
    Goal("driver_5", "Changing Seats", "Finish races as 5 different characters.", _seen("drivers"), 5),
    Goal("driver_20", "Full Grid", "Finish races as 20 different characters.", _seen("drivers"), 20),
    Goal("vehicle_5", "Test Drive", "Finish races in 5 different vehicles.", _seen("vehicles"), 5),
    Goal("vehicle_20", "Showroom", "Finish races in 20 different vehicles.", _seen("vehicles"), 20),
    Goal("coin_20", "Deep Pockets", "Finish a race holding 20 coins.", _tallied("best_coins"), 20, False),
    Goal("coin_1000", "Loose Change", "Finish races holding 1,000 coins in total.", _tallied("coins"), 1000),
    Goal("coin_10000", "Treasury", "Finish races holding 10,000 coins in total.", _tallied("coins"), 10000),
    Goal("time_trial_1", "Against the Clock", "Finish a Time Trial.", _times("game_repo_time_attack_finalize"), 1, False),
    Goal("time_trial_25", "Chasing Tenths", "Finish 25 Time Trials.", _times("game_repo_time_attack_finalize"), 25),
    Goal("free_roam", "Off the Map", "Go for a drive in Free Roam.", _times("game_repo_free_run_finalize"), 1, False),
    Goal("mission_1", "P-Switch Pressed", "Try a P-Switch mission.", _times("game_repo_mission"), 1, False),
    Goal("mission_25", "Switched On", "Try 25 P-Switch missions.", _times("game_repo_mission"), 25),
    Goal("clear_1", "Beat the Clock", "Clear a P-Switch mission.", _tallied("p_switches"), 1, False, True),
    Goal("clear_25", "On a Roll", "Clear 25 P-Switch missions.", _tallied("p_switches"), 25, True, True),
    Goal("clear_100", "Switch Hitter", "Clear 100 P-Switch missions.", _tallied("p_switches"), 100, True, True),
    Goal("clear_all", "Flipped Them All", f"Clear all {P_SWITCHES} P-Switch missions.", _tallied("p_switches"), P_SWITCHES, True, True),
    Goal("panel_1", "Knock Knock", "Hit a ? Panel.", _tallied("panels"), 1, False, True),
    Goal("panel_25", "Panel Beater", "Hit 25 ? Panels.", _tallied("panels"), 25, True, True),
    Goal("panel_75", "Wall Rider", "Hit 75 ? Panels.", _tallied("panels"), 75, True, True),
    Goal("panel_all", "No Panel Unturned", f"Hit all {PANELS} ? Panels.", _tallied("panels"), PANELS, True, True),
    Goal("medal_1", "Shiny Find", "Collect a Peach Medallion.", _tallied("medallions"), 1, False, True),
    Goal("medal_25", "Medal Hunter", "Collect 25 Peach Medallions.", _tallied("medallions"), 25, True, True),
    Goal("medal_100", "Treasure Map", "Collect 100 Peach Medallions.", _tallied("medallions"), 100, True, True),
    Goal("medal_all", "Every Last Medallion", f"Collect all {MEDALLIONS} Peach Medallions.", _tallied("medallions"), MEDALLIONS, True, True),
)


# --- Donkey Kong Bananza ----------------------------------------------------------------------------------
#
# Its save is the same GameData format, with every entry named by the MurmurHash3 of its name. Collectables sit in
# structures keyed by each object's id, and the flag that says one is collected has a hash of its own per object: a world
# banana's IsBroken (the game's play report bananamond_broken fires with that banana's id), a fossil's or treasure chest's
# IsGet. Those hashes come from the game's data list (GameDataList.Product.320) and are kept in save_map.json, which
# DonkeyKongBananza-Native/tools/make_save_map.py writes. The totals are the game's own lists: 763 bananas lie in the
# world (the official 777 also counts the 14 bought with banana chips, which the save keeps no flag for) and 696 fossils.

_BANANZA_MAP = ASSET_DIR / "switch" / "dkb" / "save_map.json"
BANANAS, FOSSILS, CHESTS = 763, 696, 50
_INT_ARRAY = 3
_MAIN_LAYERS = ("Arena", "Wild", "Forest", "Branch", "KeepOut", "Under", "Hill", "Desert", "Ice", "Resort", "Lake",
                "Race", "Melt", "Core", "Sound", "Storm", "Fort")    # each layer's main stage is <name>LevelStage
LAYERS = len(_MAIN_LAYERS)
SKILLS = 42


class _BananzaSave:
    def __init__(self, bananas: int = 0, fossils: int = 0, chests: int = 0, skills: int = 0, cleared: bool = False,
                 emerald_rush: bool = False):
        self.bananas, self.fossils, self.chests, self.skills = bananas, fossils, chests, skills
        self.cleared, self.emerald_rush = int(cleared), int(emerald_rush)


def read_bananza_save(path: Path) -> _BananzaSave:
    """Counts what a Donkey Kong Bananza save (save/0/progress.sav) holds. Raises ValueError if it isn't one."""
    want = json.loads(_BANANZA_MAP.read_text())
    names = want["names"]
    save = _GameData(path)
    skills = 0
    if names["SkillLevel"] in save.found:
        skills = sum(level > 0 for level in save.array(names["SkillLevel"], _INT_ARRAY, "i"))
    emerald_rush = False
    dlc = Path(path).with_name("emerush.sav")        # Emerald Rush (the DLC) keeps its own save beside it
    if dlc.is_file():
        try:
            emerald_rush = _GameData(dlc).found.get(names["IsClearEmerush"]) == (0, 1)
        except (ValueError, OSError):
            pass
    return _BananzaSave(save.flags_set(want["bananas"]), save.flags_set(want["fossils"]), save.flags_set(want["chests"]),
                        skills, save.found.get(names["IsClear"]) == (0, 1), emerald_rush)


class _BananzaTally:
    """What Donkey Kong Bananza's reports add up to: stage_in names each stage as you enter it."""

    def __init__(self):
        self.events: dict = {}
        self.layers: set = set()
        self.core = self.island = 0

    def add(self, event: str, report: dict) -> None:
        self.events[event] = self.events.get(event, 0) + 1
        if event != "stage_in":
            return
        stage = str(report.get("StageName") or "")
        if not stage.endswith("LevelStage"):
            return
        layer = stage[:-len("LevelStage")]
        if layer in _MAIN_LAYERS:
            self.layers.add(layer)
        self.core |= layer == "Core"
        self.island |= layer == "Island"


DONKEY_KONG_BANANZA_GOALS = (
    Goal("dk_banana_1", "Top Banana", "Find your first banana.", _tallied("bananas"), 1, False, True),
    Goal("dk_banana_50", "Bunch Builder", "Find 50 bananas.", _tallied("bananas"), 50, True, True),
    Goal("dk_banana_150", "Banana Hoard", "Find 150 bananas.", _tallied("bananas"), 150, True, True),
    Goal("dk_banana_300", "Going Bananas", "Find 300 bananas.", _tallied("bananas"), 300, True, True),
    Goal("dk_banana_500", "Banana Republic", "Find 500 bananas.", _tallied("bananas"), 500, True, True),
    Goal("dk_banana_all", "Every Last Banana", f"Find all {BANANAS} bananas hidden in the layers "
         "(the ones you buy with banana chips don't count).", _tallied("bananas"), BANANAS, True, True),
    Goal("dk_fossil_1", "Dig Site", "Find a fossil.", _tallied("fossils"), 1, False, True),
    Goal("dk_fossil_50", "Bone Collector", "Find 50 fossils.", _tallied("fossils"), 50, True, True),
    Goal("dk_fossil_200", "Amateur Palaeontologist", "Find 200 fossils.", _tallied("fossils"), 200, True, True),
    Goal("dk_fossil_400", "Museum Piece", "Find 400 fossils.", _tallied("fossils"), 400, True, True),
    Goal("dk_fossil_all", "Natural History", f"Find all {FOSSILS} fossils.", _tallied("fossils"), FOSSILS, True, True),
    Goal("dk_chest_1", "Finders Keepers", "Open a treasure chest.", _tallied("chests"), 1, False, True),
    Goal("dk_chest_25", "Treasure Hunter", "Open 25 treasure chests.", _tallied("chests"), 25, True, True),
    Goal("dk_chest_all", "X Marks Every Spot", f"Open all {CHESTS} treasure chests.", _tallied("chests"), CHESTS, True, True),
    Goal("dk_skill_1", "New Moves", "Learn a skill.", _tallied("skills"), 1, False, True),
    Goal("dk_skill_15", "Well Trained", "Learn 15 skills.", _tallied("skills"), 15, True, True),
    Goal("dk_skill_all", "Master of Bananza", f"Learn all {SKILLS} skills.", _tallied("skills"), SKILLS, True, True),
    Goal("dk_layer_3", "Going Down", "Reach 3 layers.", _seen("layers"), 3),
    Goal("dk_layer_9", "Deep Diver", "Reach 9 layers.", _seen("layers"), 9),
    Goal("dk_layer_all", "Every Layer", f"Reach all {LAYERS} layers.", _seen("layers"), LAYERS),
    Goal("dk_core", "Center of the Earth", "Reach the planet's core.", _tallied("core"), 1, False),
    Goal("dk_clear", "Roll the Credits", "Finish the story.", _tallied("cleared"), 1, False, True),
    Goal("dk_island", "Island Getaway", "Visit DK Island (DK Island & Emerald Rush).", _tallied("island"), 1, False),
    Goal("dk_emerald_rush", "Emerald Rush", "Clear Emerald Rush (DK Island & Emerald Rush).", _tallied("emerald_rush"), 1,
         False, True),
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

    def icon(self, goal: Goal) -> str:
        """The goal's icon: AchTrack's own drawing (tools/make_switch_icons.py), shown grey while locked."""
        path = ASSET_DIR / "switch" / self.key / f"{goal.name}.png"
        return str(path) if path.is_file() else ""

    def schema(self) -> list:
        return [{"name": goal.name, "display_name": goal.title, "description": goal.detail, "icon": self.icon(goal),
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
@dataclass(frozen=True)
class NativeTitle(Title):
    """A Switch game that runs as its own Mac app, with no emulator and no Ryubing folders."""
    app_name: str = ""
    save_name: str = "save/Play/account.sav"     # the save the from_save goals read, in the data folder
    save_reader: object = None                    # path -> the counts those goals measure (read_world_save if unset)
    report_tally: object = None                   # () -> a tally with add(event, report) (_WorldTally if unset)
    report_events: tuple = ("game_repo_",)        # the reports that tally counts, by the start of their event name
    raw_reports: bool = False                     # keep binary and extension values as Ryubing logs them (formats.msgpack_value)

    def app(self) -> Path | None:
        for folder in app_folders():
            if (folder / self.app_name).is_dir():
                return folder / self.app_name
        return None

    def program(self) -> str:
        """What runs the game. The app's launcher stays open beside it, and is all that runs while
        its settings window waits for you to press Play."""
        app = self.app()
        return str(app / "Contents" / "MacOS" / "dmhost") if app else ""

    def data_folder(self) -> Path:
        return Path.home() / "Library" / "Application Support" / "nxnative" / self.title_id

    def save_folder(self) -> Path | None:
        folder = self.data_folder() / "save"
        return folder if folder.is_dir() or self.app() else None

    def reports_file(self) -> Path:
        return self.data_folder() / "playreports.jsonl"

    def reads_save(self) -> bool:
        return any(goal.from_save for goal in self.goals)

    def save_file(self) -> Path:
        """The save the collectables are read from (Mario Kart World keeps one, shared by solo and two-player Free Roam)."""
        return self.data_folder() / self.save_name

    def read_save(self, path: Path):
        return (self.save_reader or read_world_save)(path)

    def new_tally(self):
        return (self.report_tally or _WorldTally)()

    def game_file(self) -> str:
        app = self.app()
        return str(app) if app else ""


MARIO_KART_NATIVE = NativeTitle(       # the same game and library entry as MARIO_KART, as its own Mac app
    key=MARIO_KART.key,
    name=MARIO_KART.name,
    title_id=MARIO_KART.title_id,
    genre=MARIO_KART.genre,
    art=MARIO_KART.art,
    goals=MARIO_KART_GOALS,
    app_name="Mario Kart 8 Deluxe.app",
    report_tally=_Totals,
    report_events=("user_data",),
    raw_reports=True,
)
MARIO_KART_WORLD = NativeTitle(
    key="mkw",
    name="Mario Kart World",
    title_id="0400C3F00006E000",
    genre="Racing",
    art="https://assets.nintendo.com/image/upload/ar_16:9,c_lpad,w_1240/b_white/f_auto/q_auto/store/software/switch2/"
        "70010000095431/0485f410193ca12f92a2f615ea608bb437540f321bd05e464593c33fefa09bf2",
    goals=MARIO_KART_WORLD_GOALS,
    app_name="Mario Kart World.app",
)
DONKEY_KONG_BANANZA = NativeTitle(
    key="dkb",
    name="Donkey Kong Bananza",
    title_id="040074A01BF12000",
    genre="Platformer",
    art="https://assets.nintendo.com/image/upload/ar_16:9,c_lpad,w_1240/b_white/f_auto/q_auto/store/software/switch2/"
        "70010000096809/3c8219fbdcd3bdd0ebd6239cf32c651fe10f2157aa7730fcc6a7d96cd27460e5",
    goals=DONKEY_KONG_BANANZA_GOALS,
    app_name="Donkey Kong Bananza.app",
    save_name="save/0/progress.sav",
    save_reader=read_bananza_save,
    report_tally=_BananzaTally,
    report_events=("stage_in", "bananamond_broken"),
)
TITLES = (MARIO_KART, MARIO_KART_NATIVE, MARIO_KART_WORLD, DONKEY_KONG_BANANZA)


def titles_of(db, appid: int) -> list:
    """Every way the Switch game behind this library entry runs (Mario Kart 8 Deluxe: Ryubing and its own app)."""
    linked = db.get_meta("ports") or {}
    return [title for title in TITLES if linked.get(title.key) == appid]


def title_of(db, appid: int) -> Title | None:
    """The Switch game behind this library entry, if any: its own app when that is installed, else Ryubing's."""
    found = titles_of(db, appid)
    return next((t for t in found if isinstance(t, NativeTitle) and t.app()), found[0] if found else None)


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


def is_native_save(path: str) -> bool:
    return any(isinstance(t, NativeTitle) and str(t.save_file()) == str(path) for t in TITLES)


def native_title_of(path) -> NativeTitle:
    """The native game a reports file or save belongs to, by the title id folder it lives in (Mario Kart World if none)."""
    folder = str(path)
    for title in TITLES:
        if isinstance(title, NativeTitle) and f"/{title.title_id}/" in folder:
            return title
    return MARIO_KART_WORLD


def parse_save(path: Path, title: NativeTitle = None) -> dict:
    """{achievement: AchState} for the goals a native game's save measures. The save keeps no usable time for each
    pickup, so a goal is dated by the save's last write, the way Ryubing's log dates its goals."""
    title = title or native_title_of(path)
    tally = title.read_save(path)
    when = int(os.stat(path).st_mtime)
    out = {}
    for goal in title.goals:
        if goal.from_save:
            value = goal.measure(tally)
            done = value >= goal.target
            out[goal.name] = AchState(goal.name, done, when if done else None,
                                      float(min(value, goal.target)) if goal.counted else None,
                                      float(goal.target) if goal.counted else None)
    return out


def parse_reports(path: Path, title: NativeTitle = None) -> dict:
    """{achievement: AchState} from a native game's reports, each dated by the report that earned it."""
    title = title or native_title_of(path)
    tally = title.new_tally()
    goals = [goal for goal in title.goals if not goal.from_save]
    earned: dict = {}
    with open(path, "rb") as f:
        lines = f.readlines()
    for line in lines:
        try:
            entry = json.loads(line)
            event = entry["event"]
            if not event.startswith(title.report_events):
                continue
            report = formats.msgpack_value(bytes.fromhex(entry["data"]), title.raw_reports)
        except (ValueError, KeyError, TypeError, AttributeError, IndexError, struct.error):
            continue                                        # the game may be writing the last line right now
        if not isinstance(report, dict):
            continue
        tally.add(event, report)
        for goal in goals:
            if goal.name not in earned and goal.measure(tally) >= goal.target:
                earned[goal.name] = _count(entry.get("time"))
    out = {}
    for goal in goals:
        value = goal.measure(tally)
        out[goal.name] = AchState(goal.name, goal.name in earned, earned.get(goal.name),
                                  float(min(value, goal.target)) if goal.counted else None,
                                  float(goal.target) if goal.counted else None)
    return out
