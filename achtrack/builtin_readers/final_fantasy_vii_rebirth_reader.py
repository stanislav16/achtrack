"""Save reader for FINAL FANTASY VII REBIRTH. Written from an AchTrack reader kit.

The save is plain fixed-layout binary, mostly zeros: a 16-byte header, one "RESD" section of
about 934 KB holding the game's state, then a run of "LOSD" sections. Nothing in it is named.

One thing in it was established beyond doubt, and it is the one thing needed: the game keeps
its own record of which achievements have been earned. The 64-bit value at 0x3DC8 has one bit
per achievement, and bit N is the achievement Steam calls ACH_N (ACH_00 .. ACH_60).

The evidence is one player's 25 save slots, written over twelve days, and that player's own
record of 32 achievements:

  - In the newest slot exactly 32 bits are set, and they are exactly those 32 achievements.
    None of the other 29 is set, and no bit above 60 is ever set.
  - Reading the slots in the order they were written, bits only ever appear, never disappear,
    and each appears where it should: "Win a battle" and "Stagger an enemy" in the very first
    slot, the four early combat ones together, "Complete Chapter N" for N = 1..8 each in the
    first slot written after that chapter, the Junon parade ones with chapter 4.

So nothing here is inferred from a pattern: every achievement is read from the game's own flag.
"""
import struct

NAME = "FINAL FANTASY VII REBIRTH"

SECTION = b"RESD" * 4
EARNED_AT = 0x3DC8
ACHIEVEMENTS = 61           # ACH_00 .. ACH_60


def can_read(filename: str, data: bytes) -> bool:
    if len(data) < 0x50000 or data[0x10:0x20] != SECTION:
        return False
    size = struct.unpack_from("<I", data, 4)[0]
    return 0x50000 <= size <= len(data)


def read(filename: str, data: bytes) -> dict:
    if not can_read(filename, data):
        raise ValueError("not a FINAL FANTASY VII REBIRTH save slot")
    flags = struct.unpack_from("<Q", data, EARNED_AT)[0]
    if flags >> ACHIEVEMENTS:
        # Bits past the last achievement have never been seen set. If they are, this file is not
        # laid out the way the reader assumes (another version of the game, perhaps) and nothing
        # read from it should be believed.
        raise ValueError("this save is not laid out the way the reader expects")
    return {
        "SectionSize": struct.unpack_from("<I", data, 4)[0],
        "AchievementFlags": flags,
        "AchievementsEarned": [n for n in range(ACHIEVEMENTS) if flags >> n & 1],
    }


def achievements(values: dict) -> dict:
    earned = values.get("AchievementsEarned")
    if not isinstance(earned, list):
        return {}
    return {"ACH_%02d" % n: n in earned for n in range(ACHIEVEMENTS)}
