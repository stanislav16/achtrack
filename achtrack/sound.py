"""Unlock chimes. Synthesised once into the cache dir and played with whatever player the system has."""
from __future__ import annotations

import math
import os
import shutil
import struct
import subprocess
import wave
from pathlib import Path

from .config import CACHE_DIR

RATE = 44100
CHIMES = {
    # (frequency Hz, start s, length s)
    "unlock": [(1318.5, 0.00, 0.9), (1975.5, 0.09, 1.1)],
    "rare": [(1046.5, 0.00, 1.0), (1318.5, 0.08, 1.0), (1568.0, 0.16, 1.0), (2093.0, 0.24, 1.4)],
    "session": [(880.0, 0.00, 0.5), (1174.7, 0.07, 0.7)],
}
VOLUME = {"unlock": 0.32, "rare": 0.34, "session": 0.18}


def _synth(path: Path, notes: list, volume: float) -> None:
    length = int((max(s + d for _, s, d in notes) + 0.05) * RATE)
    buf = [0.0] * length
    for freq, start, dur in notes:
        offset = int(start * RATE)
        for i in range(min(int(dur * RATE), length - offset)):
            t = i / RATE
            env = min(1.0, t / 0.004) * math.exp(-t * 5.0)
            tone = math.sin(2 * math.pi * freq * t) + 0.35 * math.sin(4 * math.pi * freq * t) * math.exp(-t * 9.0)
            buf[offset + i] += tone * env
    peak = max(abs(v) for v in buf) or 1.0
    frames = b"".join(struct.pack("<h", int(v / peak * volume * 32767)) for v in buf)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(frames)
    tmp.replace(path)


def chime(kind: str) -> Path:
    path = CACHE_DIR / "sounds" / f"{kind}-v1.wav"
    if not path.is_file():
        _synth(path, CHIMES[kind], VOLUME[kind])
    return path


def _player() -> list | None:
    candidates = (["afplay"], ["pw-play"], ["paplay"], ["aplay", "-q"],
                  ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"])
    for cmd in candidates:
        if shutil.which(cmd[0]):
            return cmd
    return None


def _play_windows(kind: str, custom: str) -> None:
    import winsound
    try:
        path = Path(custom).expanduser() if custom and Path(custom).expanduser().is_file() else chime(kind)
        winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except (OSError, RuntimeError):
        pass


def play(kind: str = "unlock", custom: str = "") -> None:
    if os.name == "nt":
        _play_windows(kind, custom)
        return
    player = _player()
    if not player:
        return
    try:
        path = Path(custom).expanduser() if custom and Path(custom).expanduser().is_file() else chime(kind)
        subprocess.Popen(player + [str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError:
        pass
