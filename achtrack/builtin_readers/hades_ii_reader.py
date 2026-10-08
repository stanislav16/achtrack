"""Save reader for Hades II (Supergiant "SGB1" saves). Written from an AchTrack reader kit.

Layout, little-endian: "SGB1", adler32 of everything after it, version, timestamp, location,
five counters and two flags, the list of Lua keys, two map names, then the game state as a
length-prefixed LZ4 block holding "luabins" - Lua tables written as tagged values.
"""
import struct
import zlib

NAME = "Hades II"
MAX_DEPTH = 40


class _In:
    def __init__(self, data):
        self.d, self.p = data, 0

    def take(self, n):
        if self.p + n > len(self.d):
            raise ValueError("the save ends before its data does")
        chunk = self.d[self.p:self.p + n]
        self.p += n
        return chunk

    def u8(self):
        return self.take(1)[0]

    def u32(self):
        return struct.unpack("<I", self.take(4))[0]

    def u64(self):
        return struct.unpack("<Q", self.take(8))[0]

    def f64(self):
        return struct.unpack("<d", self.take(8))[0]

    def text(self):
        return self.take(self.u32()).decode("utf-8", "replace")


def _lz4_block(src):
    """Plain LZ4 block decompression."""
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        token = src[i]
        i += 1
        length = token >> 4
        if length == 15:
            while True:
                extra = src[i]
                i += 1
                length += extra
                if extra != 255:
                    break
        out += src[i:i + length]
        i += length
        if i >= n:
            break
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        if offset == 0 or offset > len(out):
            raise ValueError("corrupt LZ4 data")
        length = token & 15
        if length == 15:
            while True:
                extra = src[i]
                i += 1
                length += extra
                if extra != 255:
                    break
        length += 4
        start = len(out) - offset
        if offset >= length:
            out += out[start:start + length]
        else:
            for k in range(length):
                out.append(out[start + k])
    return bytes(out)


def _lua_value(r, depth):
    kind = r.take(1)
    if kind == b"-":
        return None
    if kind == b"0":
        return False
    if kind == b"1":
        return True
    if kind == b"N":
        number = r.f64()
        return int(number) if number == int(number) and abs(number) < 2 ** 53 else number
    if kind == b"S":
        return r.text()
    if kind == b"T":
        if depth > MAX_DEPTH:
            raise ValueError("tables nested too deeply")
        count = r.u32() + r.u32()
        pairs = []
        for _ in range(count):
            key = _lua_value(r, depth + 1)
            pairs.append((key, _lua_value(r, depth + 1)))
        keys = [k for k, _v in pairs]
        if keys and keys == list(range(1, len(keys) + 1)):      # a Lua array
            return [v for _k, v in pairs]
        return {str(k): v for k, v in pairs}
    raise ValueError("unknown value type in the save's Lua data")


def _luabins(data):
    r = _In(data)
    return [_lua_value(r, 0) for _ in range(r.u8())]


def can_read(filename: str, data: bytes) -> bool:
    return data[:4] == b"SGB1" and len(data) > 64 and struct.unpack_from("<I", data, 8)[0] in (17, 18)


def read(filename: str, data: bytes) -> dict:
    if not can_read(filename, data):
        raise ValueError("not a Hades II save")
    if zlib.adler32(data[8:]) != struct.unpack_from("<I", data, 4)[0]:
        raise ValueError("the save's checksum doesn't match - it may be half written")
    r = _In(data)
    r.take(8)
    out = {"Version": r.u32(), "Timestamp": r.u64(), "Location": r.text()}
    out["CompletedRuns"] = r.u32()
    out["AccumulatedMetaPoints"] = r.u32()
    out["ActiveShrinePoints"] = r.u32()
    out["HeaderValue4"] = r.u32()        # meaning not established; kept under a neutral name
    out["HeaderValue5"] = r.u32()
    out["EasyMode"] = bool(r.u8())
    out["HardMode"] = bool(r.u8())
    out["LuaKeys"] = [r.text() for _ in range(r.u32())]
    out["CurrentMapName"] = r.text()
    out["StartNextMap"] = r.text()
    state = _luabins(_lz4_block(r.take(r.u32())))
    for index, table in enumerate(state):
        if isinstance(table, dict):
            out.update(table) if index == 0 else out.setdefault(f"State{index}", table)
    return out


# The names Steam gives the game's achievements, which are also the names the game uses itself.
ACHIEVEMENTS = (
    "AchClearErebus", "AchClearOceanus", "AchClearFields", "AchClearTartarus",
    "AchClearEphyra", "AchClearThessaly", "AchClearOlympus", "AchClearSummit",
    "AchCauldronSpells1", "AchBathHouse", "AchFishingPier", "AchTaverna",
    "AchForgeBonds", "AchProphecies1", "AchMaxMem", "AchAllArcana",
    "AchAllArcanaMax", "AchAllWeapons", "AchAllAspects", "AchConfideFrinos",
    "AchAllFamiliars", "AchAllFamiliarsMax", "AchEarnPrestige1", "AchAllKeepsakes",
    "AchBounties1", "AchAllRandomStreak", "AchAllRandomUnderworld", "AchBeatHecateNoArcana",
    "AchHelpOdysseus", "AchHelpDora", "AchHelpHypnos", "AchHelpArachne",
    "AchHelpNarcissusAndEcho", "AchCatchFish", "AchBeatRivalsVow1", "AchBeatTyphonWithWeapons",
    "AchBeatChronosWithArcana", "AchClearWithAllAspects", "AchClearChaosQuest", "AchNemesisCombat",
    "AchFamiliarCostumes", "AchKillWitchAsSheep", "AchSummonSiren", "AchTartarusInDress",
    "AchEarnStatue1", "AchEarnStatue2", "AchWinSecretContest", "AchTrueEnding",
    "AchEpilogue", "AchAllOtherAch",
)


def achievements(values: dict) -> dict:
    """Read from the game's own record, GameState.AchievementsUnlocked.

    The game adds an entry there, under the achievement's own name, when it awards one - the
    table does not exist at all in a save from before the first award. Seen in one player's
    saves: absent in every copy made while nothing had been earned, and holding exactly
    AchClearErebus in the first copy after the guardian of Erebus was defeated (her defeat count
    in GameState.EnemyKills going from 0 to 1 in that same save).
    """
    state = values.get("GameState")
    if not isinstance(state, dict):
        return {}
    record = state.get("AchievementsUnlocked")
    record = record if isinstance(record, dict) else {}
    return {name: record.get(name) is True for name in ACHIEVEMENTS}
