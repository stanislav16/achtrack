"""Checks the Windows-only parts of AchTrack on a real Windows machine.

    python tools/windows_selftest.py [folder for screenshots]

It makes a pretend cracked game (a folder with a steam_appid.txt and a running .exe) and a pretend
emulator achievement file, and checks AchTrack notices both; then exercises save paths, autostart,
sound and the window itself. Everything it creates is removed again.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

APPID = 99999990
failures: list = []


def check(name: str):
    def wrap(fn):
        try:
            fn()
            print(f"  ok    {name}")
        except Exception:                               # the point is to report every failure
            failures.append(name)
            detail = traceback.format_exc()
            print(f"  FAIL  {name}\n{detail}")
            print(f"::error title={name}::{detail.strip().splitlines()[-1]}")
        return fn
    return wrap


def main() -> int:
    if os.name != "nt":
        print("This test is for Windows.")
        return 2
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")
    shots = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    work = Path(tempfile.mkdtemp(prefix="achtrack-selftest-"))
    from achtrack import desktop, playtime, savefiles, savesync, scanner, sound, winhost
    from achtrack.config import CONFIG_DIR, DATA_DIR

    print(f"Config in {CONFIG_DIR}\nData in   {DATA_DIR}")

    @check("Windows' own folders are found")
    def _():
        for name in ("profile", "appdata", "local_appdata", "documents", "saved_games", "public"):
            path = getattr(winhost, name)()
            assert path and str(path), name
            print(f"          {name}: {path}")
        assert winhost.appdata().is_dir() and winhost.documents().parent.exists()

    game = work / "Games" / "Pretend Game"
    game.mkdir(parents=True)
    (game / "steam_appid.txt").write_text(str(APPID))
    exe = game / "PretendGame.exe"
    shutil.copy(Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe", exe)
    proc = subprocess.Popen([str(exe), "/c", "ping -n 40 127.0.0.1 >nul"], cwd=str(game),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.5)

    monitor = playtime.ProcessMonitor()

    @check("processes can be listed and read")
    def _():
        pids = monitor.src.pids()
        assert len(pids) > 5, pids
        assert str(os.getpid()) in pids
        args, path, _env = monitor.src.describe(str(os.getpid()))
        assert os.path.normcase(path) == os.path.normcase(sys.executable) or path.lower().endswith(".exe"), path
        assert monitor.src.status(str(os.getpid()))[0] == "R"
        mine = monitor.src.mapped(str(os.getpid()))
        assert any("python" in m.lower() for m in mine), mine[:5]

    @check("a running game is recognised by its folder's crack config")
    def _():
        found = {}
        for _ in range(3):
            found = monitor.scan({})
            if APPID in found:
                break
            time.sleep(1)
        assert APPID in found, f"saw {found}; processes matched: {monitor.seen}"
        assert os.path.normcase(found[APPID]) == os.path.normcase(str(game)), found

    @check("a game is recognised by the executable name set for it")
    def _():
        fresh = playtime.ProcessMonitor()
        assert fresh.scan({"pretendgame.exe": 4242}).get(4242, "missing") != "missing"

    @check("a game that has exited stops counting")
    def _():
        proc.kill()
        proc.wait(timeout=10)
        time.sleep(0.5)
        assert APPID not in monitor.scan({}), monitor.seen

    saves = winhost.appdata() / "Goldberg SteamEmu Saves" / str(APPID)
    made_parent = not saves.parent.exists()

    @check("an emulator's achievement file is found in AppData")
    def _():
        saves.mkdir(parents=True, exist_ok=True)
        (saves / "achievements.json").write_text(json.dumps({"ACH_ONE": {"earned": True, "earned_time": 1700000000}}))
        files = scanner.find_state_files()
        assert any(f.appid == APPID and f.emulator == "Goldberg" for f in files), [f.path for f in files]

    @check("the game folder's own appid is read")
    def _():
        assert scanner.detect_appid_in_tree(str(game)) == APPID
        assert savefiles.game_root(str(game)) == str(game)

    docs_save = winhost.documents() / "My Games" / "Pretend Game" / "Saves"

    @check("save files are found in Documents, only in the game's own folders")
    def _():
        docs_save.mkdir(parents=True, exist_ok=True)
        (docs_save / "slot1.sav").write_bytes(b"save")
        found = savefiles.candidate_files(None, str(game), ["Pretend Game"])
        assert str(docs_save / "slot1.sav") in found, found[:10]
        strangers = [f for f in found if "pretend" not in f.lower() and "my games" not in f.lower()
                     and "saved games" not in f.lower()]
        assert not strangers, strangers[:5]

    @check("save paths translate to the portable form and back")
    def _():
        layout = savesync.Layout("", str(game))
        real = str(docs_save / "slot1.sav")
        portable = layout.portable(real)
        assert portable == "user/Documents/My Games/Pretend Game/Saves/slot1.sav", portable
        assert os.path.normcase(layout.real(portable)) == os.path.normcase(real)
        roaming = str(winhost.appdata() / "Studio" / "Game" / "a.sav")
        assert layout.portable(roaming) == "user/AppData/Roaming/Studio/Game/a.sav", layout.portable(roaming)
        assert layout.portable(str(game / "save" / "x.dat")) == "game/save/x.dat"
        assert layout.real("user/../x") is None and layout.real("c/Windows:x") is None

    @check("start-with-Windows can be turned on and off")
    def _():
        before = desktop.autostart_enabled()
        desktop.set_autostart(True)
        assert desktop.autostart_enabled()
        desktop.set_autostart(False)
        assert not desktop.autostart_enabled()
        desktop.set_autostart(before)

    @check("the unlock sound is made and played")
    def _():
        assert sound.chime("unlock").is_file()
        sound.play("unlock")

    @check("dates are written without a strftime Windows lacks")
    def _():
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from achtrack import gui
        assert gui.fmt_unlock(1700000000) and gui.fmt_day(1700000000)

    @check("the update commands can be built")
    def _():
        from achtrack import updater
        assert updater.update_command(Path(r"C:\Some Folder\achtrack"))[0] == "powershell"
        assert updater.relaunch_command()[0] == "powershell"

    @check("the window opens and draws")
    def _():
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from PySide6.QtWidgets import QApplication
        from achtrack import theme
        from achtrack.config import Config
        from achtrack.gui import MainWindow, SettingsDialog
        from achtrack.notifier import Notifier, Toast, ToastData
        app = QApplication.instance() or QApplication([])
        cfg = Config()

        class Quiet:
            def request(self, *_a):
                pass

        win = MainWindow(cfg, Quiet(), Notifier(cfg), has_tray=False)
        win.resize(1240, 800)
        win.show()
        for _ in range(10):
            app.processEvents()
            time.sleep(0.05)
        assert theme.check_image(theme.palette("dark")), "tick image wasn't written"
        settings = SettingsDialog(win)
        settings.show()
        toast = Toast(ToastData(kicker="Achievement unlocked", title="It works on Windows", body="Self-test",
                                footer="AchTrack", footer_right="", rare=False, sound=None), 6)
        toast.show()
        for _ in range(6):
            app.processEvents()
            time.sleep(0.05)
        if shots:
            shots.mkdir(parents=True, exist_ok=True)
            win.grab().save(str(shots / "window.png"))
            settings.grab().save(str(shots / "settings.png"))
            toast.grab().save(str(shots / "popup.png"))

    # tidy up everything made above
    if proc.poll() is None:
        proc.kill()
    shutil.rmtree(saves, ignore_errors=True)
    if made_parent:
        shutil.rmtree(saves.parent, ignore_errors=True)
    shutil.rmtree(docs_save.parent, ignore_errors=True)
    try:
        docs_save.parent.parent.rmdir()            # "My Games", only if this test made it and it's empty
    except OSError:
        pass
    shutil.rmtree(work, ignore_errors=True)

    print(f"\n{len(failures)} failed" if failures else "\nAll passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
