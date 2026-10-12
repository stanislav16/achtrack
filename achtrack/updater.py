"""Updating AchTrack from inside the app.

AchTrack is installed from a git checkout: `git pull`, then `install.sh`. This does the same two
steps without a terminal. The installer leaves a note saying where the checkout is, since the
installed copy lives in its own virtualenv and has no other way to know.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .config import APP_ID, DATA_DIR

SOURCE_NOTE = DATA_DIR / "source"
GUESSES = ("Documents/achtrack", "Documents/achtrack-main", "achtrack", "src/achtrack", "Projects/achtrack")


def environment() -> dict:
    """git must fail rather than sit waiting for a password nobody can type."""
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "ssh -o BatchMode=yes", "LC_ALL": "C"}


WINDOWS = os.name == "nt"
INSTALLER = "install.ps1" if WINDOWS else "install.sh"


def is_checkout(folder: Path) -> bool:
    return (folder / INSTALLER).is_file() and (folder / "achtrack" / "__init__.py").is_file()


def source_dir() -> Path | None:
    """The folder AchTrack was installed from, if it can still be found."""
    candidates = []
    if os.environ.get("ACHTRACK_SOURCE"):
        candidates.append(Path(os.environ["ACHTRACK_SOURCE"]).expanduser())
    try:
        # utf-8-sig: Windows PowerShell puts a byte-order mark in front of what it writes, and
        # with it left on the path names a folder that doesn't exist.
        candidates.append(Path(SOURCE_NOTE.read_text(encoding="utf-8-sig").strip()).expanduser())
    except OSError:
        pass
    candidates += [Path.home() / guess for guess in GUESSES]
    for folder in candidates:
        if str(folder) and is_checkout(folder):
            return folder
    return None


def problem(folder: Path | None) -> str:
    """Why updating can't work from here - "" when it can."""
    if getattr(sys, "frozen", False) and WINDOWS:
        return ("This is the ready-made AchTrack.exe, which can't rebuild itself. Download a newer "
                "build, or install from the source with install.ps1 to get updates here.")
    if folder is None:
        return (f"Can't find the folder AchTrack was installed from - it may have been moved or deleted. "
                f"Updates are fetched into that folder with git, so it has to stay. Clone the repository "
                f"somewhere permanent and run {INSTALLER} from there once; it will be remembered.")
    if not shutil.which("git"):
        return "git isn't installed, so there's no way to fetch updates."
    if not (folder / ".git").exists():
        return (f"{folder} was downloaded as a ZIP, not cloned with git, so it can't fetch updates. "
                f"Clone the repository instead and install from that.")
    return ""


def _git(folder: Path, *args: str, timeout: float = 60) -> tuple:
    try:
        done = subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True,
                              timeout=timeout, env=environment())
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)
    return done.returncode, (done.stdout if done.returncode == 0 else done.stderr or done.stdout).strip()


def check(folder: Path) -> dict:
    """Asks the server what's new. {"error", "behind", "changes": [subject, ...], "current"}."""
    status = {"error": "", "behind": 0, "changes": [], "current": ""}
    code, out = _git(folder, "fetch", "--quiet", timeout=90)
    if code != 0:
        refused = "permission denied" in out.lower() or "authentication" in out.lower() \
            or "could not read" in out.lower()
        status["error"] = ("GitHub refused the connection - this machine's key or login for the "
                           "repository isn't working." if refused else
                           f"Couldn't reach the server: {out.splitlines()[-1] if out else 'no answer'}")
        return status
    _code, status["current"] = _git(folder, "log", "-1", "--format=%h %s")
    code, out = _git(folder, "log", "--format=%s", "HEAD..@{upstream}")
    if code != 0:
        status["error"] = "This copy isn't following a branch on the server, so there's nothing to compare with."
        return status
    status["changes"] = [line for line in out.splitlines() if line.strip()]
    status["behind"] = len(status["changes"])
    return status


def update_command(folder: Path) -> list:
    """One shell command that pulls and reinstalls.

    It goes through a login shell on purpose: an app started from the dock has a bare PATH, and
    the installer has to find the same python3 the terminal would.
    """
    if WINDOWS:
        script = (f"Set-Location -LiteralPath '{str(folder).replace(chr(39), chr(39) * 2)}'; git pull --ff-only; "
                  "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; & .\\install.ps1 -Quiet; exit $LASTEXITCODE")
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script]
    shell = os.environ.get("SHELL") or ("/bin/zsh" if sys.platform == "darwin" else "/bin/bash")
    script = f"cd {shlex.quote(str(folder))} && git pull --ff-only && bash install.sh"
    return [shell, "-lc", script]


def relaunch_command() -> list:
    """How to start AchTrack again once the running copy has quit."""
    if WINDOWS:
        from .desktop import windows_program
        program = windows_program()
        start = "Start-Process -FilePath '{}'".format(program[0].replace("'", "''"))
        if program[1:]:
            start += " -ArgumentList " + ",".join("'{}'".format(a) for a in program[1:])
        script = f"Wait-Process -Id {os.getpid()} -ErrorAction SilentlyContinue; Start-Sleep -Milliseconds 500; {start}"
        return ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script]
    bundle = Path.home() / "Applications" / "AchTrack.app"
    if sys.platform == "darwin" and bundle.is_dir():
        start = f"open -n {shlex.quote(str(bundle))}"
    else:
        exe = shutil.which(APP_ID) or str(Path.home() / ".local" / "bin" / APP_ID)
        start = f"{shlex.quote(exe)} >/dev/null 2>&1"
    # Wait for this copy to let go of its single-instance lock before the new one starts.
    return ["/bin/sh", "-c", f"while kill -0 {os.getpid()} 2>/dev/null; do sleep 0.3; done; sleep 0.5; {start} &"]
