"""Save reader for FINAL FANTASY VII REBIRTH. Written from an AchTrack reader kit.

The save is plain fixed-layout binary, mostly zeros: a 16-byte header, one "RESD" section of
about 934 KB holding the game's state, then a run of "LOSD" sections. Nothing in it is named,
so this reader decodes only what could be established from evidence, and says how.

What was established, from one player's 25 save slots made over twelve days and the dates on
which that player recorded each "Complete Chapter N" achievement:

  The 32-bit value at 0x3DC8 is a set of flags. Bit N (N = 1..8) is clear in every slot saved
  before chapter N was finished and set in every slot saved after - eight chapters, eight bits,
  each appearing in the first save made after that chapter's achievement, never earlier. Bits
  16 and up belong to something else and fill in over the first few chapters; bit 15 is always
  set.

Chapters 9 to 14 use bits 9 to 14 on the strength of that pattern: it has not been seen happen,
because the player had not got that far. If it is wrong the rule simply never fires.
"""
import struct

NAME = "FINAL FANTASY VII REBIRTH"

SECTION = b"RESD" * 4
FLAGS_AT = 0x3DC8
PROGRESS_ID_AT = 0x450BC
CHAPTERS = 14


def can_read(filename: str, data: bytes) -> bool:
    if len(data) < 0x50000 or data[0x10:0x20] != SECTION:
        return False
    size = struct.unpack_from("<I", data, 4)[0]
    return 0x50000 <= size <= len(data)


def read(filename: str, data: bytes) -> dict:
    if not can_read(filename, data):
        raise ValueError("not a FINAL FANTASY VII REBIRTH save slot")
    flags = struct.unpack_from("<I", data, FLAGS_AT)[0]
    if not flags & 0x8000:
        # Every slot examined has this bit set; a file without it is not laid out the way this
        # reader assumes (an empty slot, or a different version of the game).
        raise ValueError("this save is not laid out the way the reader expects")
    return {
        "SectionSize": struct.unpack_from("<I", data, 4)[0],
        "StoryFlags": flags,
        "ChaptersCompleted": [n for n in range(1, CHAPTERS + 1) if flags >> n & 1],
        # Changes exactly when the chapter does; what the number itself means is not known.
        "ProgressId": struct.unpack_from("<I", data, PROGRESS_ID_AT)[0],
    }


def achievements(values: dict) -> dict:
    """ACH_01 .. ACH_14 are "Complete Chapter 1" .. "Complete Chapter 14". Nothing else is judged."""
    done = values.get("ChaptersCompleted")
    if not isinstance(done, list):
        return {}
    return {"ACH_%02d" % n: n in done for n in range(1, CHAPTERS + 1)}
