"""Starting games from AchTrack.

AchTrack doesn't run games itself - Steam, Heroic and CrossOver already know how, with all their
per-game settings. The Play button hands the game to whichever of them it belongs to:

    steam://rungameid/<appid>         Steam, including the Mac's with NotProton
    heroic://launch?appName=…         a game installed or added in Heroic
    a CrossOver launcher app          a Windows program installed in a CrossOver bottle
    the program itself                a Windows .exe on Windows, a script, anything you pick

A program AchTrack starts directly carries ACHTRACK_APPID in its environment, so it is counted
as that game from its first second.

How a game starts is remembered per machine (the `launch` column, never synced): "auto", or
"<kind>:<value>" when you chose it.
"""
from __future__ import annotations

import os
import plistlib
import re
import shlex
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from . import scanner

IS_MAC = sys.platform == "darwin"
IS_WINDOWS = os.name == "nt"
CROSSOVER_APPS = Path.home() / "Applications" / "CrossOver"


@dataclass
class Launch:
    kind: str           # steam | heroic | app | program
    value: str          # the appid, the Heroic "runner/appName", the .app path or the program path
    label: str          # how the menu describes it

    def stored(self) -> str:
        return f"{self.kind}:{self.value}"


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def from_stored(text: str) -> Launch | None:
    kind, sep, value = (text or "").partition(":")
    if not sep or not value:
        return None
    labels = {"steam": "Steam", "heroic": "Heroic", "app": "CrossOver", "program": os.path.basename(value)}
    return Launch(kind, value, labels.get(kind, value))


# --- what is installed where ----------------------------------------------------------------------

def steam_installed(appid: int) -> bool:
    """Whether Steam on this machine has the game installed (any library)."""
    if appid <= 0:
        return False
    for lib in scanner.steam_libraries():
        if (Path(lib) / "steamapps" / f"appmanifest_{appid}.acf").is_file():
            return True
    return False


def heroic_entry(names: list) -> Launch | None:
    """A Heroic game whose title is one of `names`."""
    wanted = {_key(n) for n in names if _key(n)}
    for entry in scanner.heroic_installed():
        if _key(entry["title"]) in wanted and entry.get("app_name"):
            return Launch("heroic", f"{entry['store']}/{entry['app_name']}", "Heroic")
    return None


def crossover_app(exe_names: list, names: list) -> Launch | None:
    """The launcher app CrossOver made for a program, found by what it starts or by its name."""
    if not IS_MAC or not CROSSOVER_APPS.is_dir():
        return None
    from .playtime import crossover_hosted
    exes = {os.path.basename(e).lower() for e in exe_names if e}
    wanted = {_key(n) for n in names if _key(n)}
    for app in sorted(CROSSOVER_APPS.rglob("*.app")):
        target = ""
        try:
            with open(app / "Contents" / "Info.plist", "rb") as f:
                executable = plistlib.load(f).get("CFBundleExecutable") or ""
            target, _bottle = crossover_hosted(str(app / "Contents" / "MacOS" / executable))
        except (OSError, ValueError):
            pass
        if (target and re.split(r"[\\/]", target)[-1].lower() in exes) or _key(app.stem) in wanted:
            return Launch("app", str(app), "CrossOver")
    return None


def program(game) -> Launch | None:
    """On Windows a game is just its .exe."""
    if not IS_WINDOWS or not game["game_dir"] or not game["exe_name"]:
        return None
    for exe in game["exe_name"].split(";"):
        path = os.path.join(game["game_dir"], exe.strip())
        if exe.strip() and os.path.isfile(path):
            return Launch("program", path, os.path.basename(path))
    return None


def best(db, appid: int) -> Launch | None:
    """How this game starts on this machine - what you chose, or the best guess."""
    game = db.game(appid)
    if game is None:
        return None
    chosen = from_stored(game["launch"] or "") if "launch" in game.keys() else None
    if chosen:
        return chosen
    names = [game["name"] or ""]
    exes = [e.strip() for e in (game["exe_name"] or "").split(";") if e.strip()]
    if steam_installed(appid) or (game["steam_owned"] and appid > 0 and not exes):
        return Launch("steam", str(appid), "Steam")
    return heroic_entry(names) or crossover_app(exes, names) or program(game) \
        or (Launch("steam", str(appid), "Steam") if appid > 0 and game["steam_owned"] else None)


# --- starting -------------------------------------------------------------------------------------

def _open(target: str) -> None:
    if IS_WINDOWS:
        os.startfile(target)                                    # noqa: S606 - a URL or an .app the user picked
    else:
        subprocess.Popen(["open" if IS_MAC else "xdg-open", target], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)


def start(launch: Launch, appid: int) -> None:
    """Starts the game. Raises OSError when it can't."""
    if launch.kind == "steam":
        _open(f"steam://rungameid/{int(launch.value)}")
    elif launch.kind == "heroic":
        runner, _, app_name = launch.value.partition("/")
        _open("heroic://launch?" + urllib.parse.urlencode({"appName": app_name, "runner": runner}))
    elif launch.kind == "app":
        subprocess.Popen(["open", launch.value], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif launch.kind == "program":
        path = launch.value
        if not os.path.exists(path):
            raise OSError(f"{path} isn't there any more")
        env = {**os.environ, "ACHTRACK_APPID": str(appid)}
        if IS_MAC and path.endswith(".app"):
            subprocess.Popen(["open", path], env=env)
            return
        command = [path] if os.access(path, os.X_OK) or IS_WINDOWS else ["/bin/sh", path]
        subprocess.Popen(command, cwd=os.path.dirname(path) or None, env=env, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True,
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    else:
        raise OSError(f"don't know how to start a game with {launch.kind!r}")


def describe(launch: Launch | None) -> str:
    if launch is None:
        return "not set"
    return {"steam": "through Steam", "heroic": "through Heroic", "app": "through CrossOver"}.get(
        launch.kind, shlex.quote(launch.value) if launch.kind == "program" else launch.label)
