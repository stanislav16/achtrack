import json
import os
import shutil
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from achtrack import emulators, engine as E, launcher, ports, savesync, switch, sync
from achtrack.config import Config
from achtrack.db import Database

TITLE = switch.MARIO_KART


def report(**values) -> str:
    # Written the way Ryubing writes it, including the NUL on the end of the room's name.
    fields = {"GrandPrixTrophy": "0" * 60, "TotalRaceNum": 0, "OpenGoldMario": 0,
              "OpenBodyFlags": {"TypeCode": 0, "Value": "0x0000014A0080C829"},
              "OpenTireFlags": {"TypeCode": 0, "Value": "0x000000000000C829"},
              "OpenWingFlags": {"TypeCode": 0, "Value": "0x0000000000004829"}, **values}
    return ("00:00:04.114 |I| HLE.OsThread.22 ServicePrepo ProcessPlayReport: \nPlayReport log:\n Kind: Normal\n"
            " Pid: 127\n ApplicationVersion: 0\n UserId: 00000000000000010000000000000000\n Room: user_data\0\n"
            f" Report: {json.dumps(fields, indent=4)}\n\n")


LOADED = "00:00:00.562 |I| Loader Start: Application Loaded: Mario Kart 8 Deluxe v1.7.1 [0100152000022000] [32-bit]\n"
MII = "00:00:00.376 |I| Loader Start: Application Loaded: Firmware Application miiEdit v1.0.0 [0100000000001009] [64-bit]\n"


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        self.logs = self.home / "Logs"
        self.logs.mkdir()
        self.data = self.home / "Ryujinx"
        for target, value in (("data_dir", lambda: self.data), ("logs_dirs", lambda: (self.logs,))):
            patcher = mock.patch.object(switch, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def log(self, text: str, name: str = "Ryujinx_1.3.3_2026-10-31_22-00-01.log") -> str:
        path = self.logs / name
        path.write_bytes(text.encode())
        return str(path)

    def install_save(self, number: int = 1) -> Path:
        """The system's save index, with the game's user save and its device save."""
        def entry(kind, save):
            key = struct.pack("<Q", int(TITLE.title_id, 16)) + bytes(24) + bytes([kind]) + bytes(31)
            return b"IMEN" + struct.pack("<II", 64, 64) + key + struct.pack("<Q", save) + bytes(56)
        index = self.data / "bis/system/save/8000000000000000/0"
        index.mkdir(parents=True)
        (index / "imkvdb.arc").write_bytes(b"IMKV" + bytes(4) + struct.pack("<I", 2) + entry(3, 2) + entry(1, number))
        folder = self.data / f"bis/user/save/{number:016x}"
        (folder / "0").mkdir(parents=True)
        (folder / "0" / "userdata.dat").write_bytes(b"SUTC")
        (folder / ".lock").touch()
        return folder


class Reports(Sandbox):
    def test_a_real_report_is_read_despite_the_nul_after_the_room(self):
        path = self.log(LOADED + report(TotalRaceNum=3))
        self.assertEqual(switch.reports_in(path)[TITLE.title_id]["TotalRaceNum"], 3)

    def test_a_report_belongs_to_the_game_loaded_before_it(self):
        path = self.log(MII + report(TotalRaceNum=9))
        self.assertEqual(switch.parse_log(Path(path)), {})

    def test_the_newest_report_wins_and_only_new_text_is_read(self):
        path = self.log(LOADED + report(TotalRaceNum=1))
        switch.reports_in(path)
        with open(path, "a") as f:
            f.write(report(TotalRaceNum=50))
        states = switch.parse_log(Path(path))
        self.assertTrue(states["race_50"].unlocked)
        self.assertEqual(states["race_250"].cur, 50)

    def test_a_report_still_being_written_waits(self):
        text = LOADED + report(TotalRaceNum=1)
        cut = len(text) - 20
        path = self.log(text[:cut])
        self.assertNotIn(TITLE.title_id, switch.reports_in(path))
        with open(path, "a") as f:
            f.write(text[cut:])
        self.assertEqual(switch.reports_in(path)[TITLE.title_id]["TotalRaceNum"], 1)

    def test_a_fresh_save_has_earned_nothing(self):
        states = switch.parse_log(Path(self.log(LOADED + report())))
        self.assertEqual(len(states), len(TITLE.goals))
        self.assertFalse(any(s.unlocked for s in states.values()))
        self.assertEqual(states["parts_30"].cur, 22)     # the parts the game starts you with

    def test_trophies_count_whichever_digit_a_trophy_is(self):
        digits = "3120" + "0" * 56
        states = TITLE.states({"GrandPrixTrophy": digits}, 1)
        self.assertTrue(states["trophy_1"].unlocked)
        self.assertEqual(states["trophy_12"].cur, 3)

    def test_parse_file_sends_ryubing_logs_here(self):
        path = self.log(LOADED + report(TotalRaceNum=1))
        self.assertTrue(emulators.parse_file(Path(path))["race_1"].unlocked)


class Saves(Sandbox):
    def test_the_save_index_says_which_folder_is_the_game(self):
        folder = self.install_save(7)
        self.assertEqual(TITLE.save_folder(), folder)

    def test_no_index_means_it_has_not_run_here(self):
        self.assertIsNone(TITLE.save_folder())

    def test_a_backup_holds_the_save_without_the_lock(self):
        self.install_save()
        db = Database(self.home / "t.db")
        engine = E.Engine(Config(), db)
        appid = engine.link_switch(TITLE)
        files = savesync.collect(db, appid, savesync.find_layout(db, appid))
        self.assertEqual([os.path.basename(real) for real, _portable in files], ["userdata.dat"])
        self.assertEqual(savesync.identity(db, appid), "mk8d")


class Library(Sandbox):
    def engine(self):
        return E.Engine(Config(), Database(self.home / "t.db"))

    def test_nothing_appears_until_the_game_has_run_here(self):
        engine = self.engine()
        engine.discover_switch()
        self.assertEqual(engine.db.games(include_hidden=True), [])

    def test_the_game_joins_with_its_goals_and_keeps_one_entry(self):
        self.install_save()
        engine = self.engine()
        first = engine.link_switch(TITLE)
        self.assertEqual(engine.link_switch(TITLE), first)
        game = engine.db.game(first)
        self.assertEqual((game["platform"], game["name"]), ("switch", TITLE.name))
        self.assertLess(first, 0)
        self.assertEqual(len(engine.db.achievement_names(first)), len(TITLE.goals))
        self.assertIs(ports.port_of(engine.db, first), TITLE)

    def test_an_earned_goal_is_unlocked_and_steam_is_never_asked(self):
        self.install_save()
        path = self.log(LOADED + report(TotalRaceNum=1))
        engine = self.engine()
        with mock.patch.object(E.steam_api, "fetch_app_info") as asked:
            engine.discover_switch()
            engine.ensure_meta(engine.port_game(TITLE), force=True)
        appid = engine.port_game(TITLE)
        self.assertEqual(engine.db.unlocked_names(appid), {"race_1"})
        self.assertEqual(engine.sources[path]["emulator"], switch.EMULATOR)
        asked.assert_not_called()

    def test_a_log_ryubing_removed_is_forgotten_but_its_unlocks_stay(self):
        self.install_save()
        path = self.log(LOADED + report(TotalRaceNum=1))
        engine = self.engine()
        engine.discover_switch()
        os.remove(path)
        engine.discover_switch()
        self.assertNotIn(path, engine.sources)
        self.assertEqual(engine.db.unlocked_names(engine.port_game(TITLE)), {"race_1"})

    def test_device_sync_doesnt_treat_it_as_a_steam_game(self):
        self.install_save()
        engine = self.engine()
        appid = engine.link_switch(TITLE)
        self.assertEqual(sync.game_key(engine.db.game(appid)), f"switch:{TITLE.name}")

    def test_an_entry_from_another_device_is_reused(self):
        self.install_save()
        engine = self.engine()
        engine.db.ensure_game(-5)
        engine.db.update_game(-5, platform="switch", name=TITLE.name)
        self.assertEqual(engine.link_switch(TITLE), -5)


class Playing(Sandbox):
    def engine(self, open_files):
        engine = E.Engine(Config(), Database(self.home / "t.db"))
        engine.monitor = mock.Mock(seen={})
        engine.monitor.files_open_in.return_value = open_files
        return engine

    def test_the_emulator_open_with_no_game_isnt_playing(self):
        self.install_save()
        engine = self.engine({"9": ["/some/other/file"]})
        engine.link_switch(TITLE)
        self.assertEqual(engine.switch_running(), {})

    def test_the_game_is_playing_while_its_save_is_held_open(self):
        folder = self.install_save()
        engine = self.engine({"9": [str(folder / ".lock")]})
        appid = engine.link_switch(TITLE)
        self.assertEqual(engine.switch_running(), {appid: None})
        self.assertEqual(engine.monitor.seen[appid][0][0], "9")

    def test_this_mac_can_list_a_process_open_files(self):
        monitor = E.ProcessMonitor()
        if not hasattr(monitor.src, "open_files"):
            self.skipTest("not on this platform")
        with tempfile.NamedTemporaryFile() as held:
            found = monitor.src.open_files(str(os.getpid()))
            self.assertIn(os.path.realpath(held.name), [os.path.realpath(p) for p in found])


class Starting(Sandbox):
    def test_play_starts_the_file_in_ryubings_game_folder(self):
        self.install_save()
        games = self.home / "Switch"
        games.mkdir()
        nsp = games / f"Mario Kart 8 Deluxe [{TITLE.title_id}].nsp"
        nsp.touch()
        self.data.mkdir(exist_ok=True)
        (self.data / "Config.json").write_text(json.dumps({"game_dirs": [str(games)]}))
        engine = E.Engine(Config(), Database(self.home / "t.db"))
        appid = engine.link_switch(TITLE)
        how = launcher.best(engine.db, appid)
        self.assertEqual((how.kind, how.value), ("ryubing", str(nsp)))
        with mock.patch.object(launcher.subprocess, "Popen") as popen, \
                mock.patch.object(switch, "app", return_value=Path("/Applications/Ryujinx.app")):
            launcher.start(how, appid)
        self.assertEqual(popen.call_args[0][0], ["open", "-a", "/Applications/Ryujinx.app", "--args", str(nsp)])

    def test_no_file_no_launch(self):
        self.install_save()
        engine = E.Engine(Config(), Database(self.home / "t.db"))
        self.assertIsNone(launcher.best(engine.db, engine.link_switch(TITLE)))


if __name__ == "__main__":
    unittest.main()
