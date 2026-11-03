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


def packed(fields: dict) -> str:
    """A report's fields as the game writes them: MessagePack, in hex."""
    def pack(value) -> bytes:
        if isinstance(value, bool):
            return b"\xc3" if value else b"\xc2"
        if isinstance(value, int):
            return b"\xce" + value.to_bytes(4, "big")
        if isinstance(value, str):
            return bytes([0xd9, len(value)]) + value.encode()
        if isinstance(value, list):
            return bytes([0x90 | len(value)]) + b"".join(pack(v) for v in value)
        return bytes([0x80 | len(value)]) + b"".join(pack(k) + pack(v) for k, v in value.items())
    return pack(fields).hex()


class World(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        patcher = mock.patch.object(Path, "home", return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        folders = mock.patch.object(switch, "app_folders", return_value=(self.home / "Applications",))
        folders.start()
        self.addCleanup(folders.stop)
        self.title = switch.MARIO_KART_WORLD
        self.title.data_folder().mkdir(parents=True)

    def write(self, *reports) -> Path:
        path = self.title.reports_file()
        lines = [json.dumps({"time": when, "event": event, "data": packed(fields)}) for when, event, fields in reports]
        path.write_text("\n".join(lines) + "\n")
        return path

    def race(self, when=100, rank=2, course="Race_FirstCircuit_A_00", driver=1, vehicle=1, coins=16, retired=False):
        return (when, "game_repo_race_finalize", {"CourseName": course, "Rank": rank, "CoinNum": coins, "DriverId": driver,
                                                  "VehicleId": vehicle, "IsRetire": retired})

    def install_app(self) -> Path:
        app = self.home / "Applications" / "Mario Kart World.app"
        app.mkdir(parents=True)
        return app

    def test_rank_counts_from_zero(self):
        states = emulators.parse_file(self.write(self.race(rank=1)))
        self.assertTrue(states["race_1"].unlocked)
        self.assertFalse(states["win_1"].unlocked)
        self.assertTrue(emulators.parse_file(self.write(self.race(rank=0)))["win_1"].unlocked)

    def test_a_goal_is_dated_by_the_report_that_earned_it(self):
        states = emulators.parse_file(self.write(self.race(when=100, rank=3), self.race(when=200, rank=0), self.race(when=300, rank=0)))
        self.assertEqual(states["race_1"].unlocked_at, 100)
        self.assertEqual(states["win_1"].unlocked_at, 200)

    def test_a_race_you_quit_earns_nothing(self):
        states = emulators.parse_file(self.write(self.race(rank=0, retired=True)))
        self.assertFalse(states["race_1"].unlocked)

    def test_progress_adds_up_across_reports(self):
        states = emulators.parse_file(self.write(self.race(coins=16, driver=1), self.race(coins=4, driver=2), self.race(coins=1, driver=2)))
        self.assertEqual((states["coin_1000"].cur, states["coin_1000"].max), (21.0, 1000.0))
        self.assertEqual(states["driver_5"].cur, 2.0)

    def test_a_course_counts_once_however_it_is_run(self):
        states = emulators.parse_file(self.write(self.race(course="Race_PeachBeach_A_00"), self.race(course="Race_PeachBeach_B_00"),
                                                 self.race(course="LongRally_Central_00")))
        self.assertEqual(states["course_5"].cur, 1.0)

    def test_a_connected_race_counts_for_the_course_it_ends_on(self):
        states = emulators.parse_file(self.write(self.race(course="InterStageEx1_FirstCircuitToWestCity_B_00"),
                                                 self.race(course="InterStageEx2_WestCityToCoaster_A_00"),
                                                 self.race(course="Race_WestCity_A_00")))
        self.assertEqual(states["course_5"].cur, 2.0)

    def test_a_cup_counts_first_place_from_one(self):
        cup = lambda rank, points, cup_id=7: (200, "game_repo_grandprix_finalize", {"CupId": cup_id, "Class": 0, "MaxPoint": points, "MaxRank": rank})
        states = emulators.parse_file(self.write(cup(1, 57), cup(1, 57), cup(1, 60, 8), cup(2, 40, 9)))
        self.assertEqual(states["cup_25"].cur, 4.0)
        self.assertEqual(states["gold_8"].cur, 2.0)
        self.assertTrue(states["gold_1"].unlocked)
        self.assertTrue(states["perfect"].unlocked)
        self.assertFalse(emulators.parse_file(self.write(cup(2, 57)))["gold_1"].unlocked)

    def test_last_to_first_needs_the_win_and_the_start_from_24th(self):
        from_last = (101, "game_repo_rank_change_in_race", {"RankChangeInfos": [{"BeforeRank": 24, "AfterRank": 23}]})
        self.assertTrue(emulators.parse_file(self.write(self.race(rank=0), from_last))["comeback"].unlocked)
        self.assertFalse(emulators.parse_file(self.write(self.race(rank=4), from_last))["comeback"].unlocked)

    def test_a_half_written_line_and_other_reports_are_skipped(self):
        path = self.write((50, "erepo_region", {"Region": 1}), self.race())
        with open(path, "a") as f:
            f.write('{"time":300,"event":"game_repo_race_fin')
        self.assertTrue(emulators.parse_file(path)["race_1"].unlocked)
        self.assertEqual(emulators.parse_file(path)["race_25"].cur, 1.0)

    def engine(self):
        return E.Engine(Config(), Database(self.home / "t.db"))

    def test_it_joins_the_library_once_its_app_is_installed(self):
        engine = self.engine()
        with mock.patch.object(switch, "TITLES", (self.title,)):
            engine.discover_switch()
            self.assertEqual(engine.db.games(include_hidden=True), [])
            self.install_app()
            engine.discover_switch()
        games = engine.db.games(include_hidden=True)
        self.assertEqual([g["name"] for g in games], ["Mario Kart World"])
        self.assertEqual(games[0]["platform"], switch.PLATFORM)
        self.assertEqual(len(engine.db.achievement_names(games[0]["appid"])), len(switch.MARIO_KART_WORLD_GOALS))

    def test_its_reports_unlock_goals_in_the_library(self):
        self.install_app()
        self.write(self.race(rank=0))
        engine = self.engine()
        with mock.patch.object(switch, "TITLES", (self.title,)):
            engine.discover_switch()
        appid = engine.db.games(include_hidden=True)[0]["appid"]
        self.assertIn("win_1", engine.db.unlocked_names(appid))

    def write_save(self, medallions=(), panels=(), missions=()) -> Path:
        """A Mario Kart World save holding just the lists AchTrack reads. missions: (name hash, times cleared)."""
        lists = [(switch._UINT_ARRAY, switch._MISSION_IDS, "I", [m for m, _ in missions]),
                 (switch._UINT_ARRAY, switch._MISSION_CLEARS, "I", [n for _, n in missions]),
                 (switch._UINT64_ARRAY, switch._MEDALLION_IDS, "Q", list(medallions)),
                 (switch._UINT64_ARRAY, switch._PANEL_IDS, "Q", list(panels))]
        table_end = 0x28 + 8 * (len(lists) + 2)
        pairs, blobs, at, kind = [], b"", table_end, None
        for list_kind, name, fmt, values in lists:
            if list_kind != kind:
                pairs.append((0, list_kind))
                kind = list_kind
            values = values + [0] * (10 - len(values))      # the game's lists are fixed-size, padded with zeros
            blob = struct.pack("<I%d%s" % (len(values), fmt), len(values), *values)
            pairs.append((name, at))
            blobs += blob
            at += len(blob)
        data = b"\x04\x03\x02\x01" + struct.pack("<II", 1, table_end) + bytes(0x28 - 12)
        data += b"".join(struct.pack("<II", *p) for p in pairs) + blobs
        path = self.title.save_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_the_save_counts_collectables_and_only_cleared_missions(self):
        path = self.write_save(medallions=(11, 12, 12), panels=(21,), missions=((31, 1), (32, 0), (33, 2)))
        tally = switch.read_world_save(path)
        self.assertEqual((tally.medallions, tally.panels, tally.p_switches), (2, 1, 2))
        states = emulators.parse_file(path)
        self.assertTrue(states["medal_1"].unlocked)
        self.assertEqual((states["medal_25"].cur, states["medal_25"].max), (2.0, 25.0))
        self.assertTrue(states["panel_1"].unlocked)
        self.assertTrue(states["clear_1"].unlocked)
        self.assertNotIn("race_1", states)                 # the reports' goals are the reports' to say

    def test_the_reports_leave_the_save_goals_alone(self):
        states = emulators.parse_file(self.write(self.race()))
        self.assertNotIn("medal_1", states)

    def test_a_file_that_is_not_a_save_is_refused(self):
        path = self.title.save_file()
        path.parent.mkdir(parents=True)
        path.write_bytes(b"not a save")
        with self.assertRaises(ValueError):
            emulators.parse_file(path)

    def test_its_save_unlocks_collectables_in_the_library(self):
        self.install_app()
        self.write(self.race(rank=0))
        self.write_save(medallions=(1,), missions=((5, 1),))
        engine = self.engine()
        with mock.patch.object(switch, "TITLES", (self.title,)):
            engine.discover_switch()
        appid = engine.db.games(include_hidden=True)[0]["appid"]
        self.assertTrue({"win_1", "medal_1", "clear_1"} <= engine.db.unlocked_names(appid))
        self.assertNotIn("panel_1", engine.db.unlocked_names(appid))

    def test_play_opens_the_app_past_its_settings_window(self):
        app = self.install_app()
        engine = self.engine()
        with mock.patch.object(switch, "TITLES", (self.title,)):
            appid = engine.link_switch(self.title)
            found = launcher.best(engine.db, appid)
            self.assertEqual(found.stored(), f"native:{app}")
            self.assertEqual(launcher.describe(found), "natively")
            with mock.patch.object(launcher.subprocess, "Popen") as popen:
                launcher.start(found, appid)
        self.assertEqual(popen.call_args[0][0], ["open", str(app), "--args", "--no-launcher"])

    def test_play_says_so_when_the_app_is_gone(self):
        with self.assertRaises(OSError):
            launcher.start(launcher.Launch("native", str(self.home / "gone.app"), "Native app"), 1)

    def test_it_is_playing_only_while_the_game_itself_runs(self):
        app = self.install_app()
        engine = self.engine()
        host = str(app / "Contents" / "MacOS" / "dmhost")
        launcher_program = str(app / "Contents" / "MacOS" / "nxnative")
        with mock.patch.object(switch, "TITLES", (self.title,)):
            appid = engine.link_switch(self.title)
            engine.monitor.cache = {("1", 0): mock.Mock(ignore=False, exe=launcher_program)}
            self.assertEqual(engine.switch_running(), {})
            engine.monitor.cache[("2", 0)] = mock.Mock(ignore=False, exe=host)
            self.assertEqual(engine.switch_running(), {appid: None})


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        self.logs = self.home / "Logs"
        self.logs.mkdir()
        self.data = self.home / "Ryujinx"
        titles = mock.patch.object(switch, "TITLES", (TITLE,))
        titles.start()
        self.addCleanup(titles.stop)
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
                mock.patch.object(switch, "close_emulator"), \
                mock.patch.object(switch, "app", return_value=Path("/Applications/Ryujinx.app")):
            launcher.start(how, appid)
        self.assertEqual(popen.call_args[0][0], ["open", "-n", "-a", "/Applications/Ryujinx.app", "--args", str(nsp)])

    def test_ryubing_stopped_after_a_game_counts_as_idle(self):
        self.log(LOADED + report() + "00:05:54.296 |N| GUI.RenderThread Application ReloadConfig: Loading\n")
        self.assertTrue(switch.emulator_idle())

    def test_ryubing_just_opened_counts_as_idle(self):
        self.log("00:00:00.1 |I| Configuration LogValueChange: ResScale set to: 1\n")
        self.assertTrue(switch.emulator_idle())

    def test_ryubing_with_something_open_isnt_idle(self):
        self.log("00:00:00.1 |N| Application ReloadConfig: Loading\n" + MII)
        self.assertFalse(switch.emulator_idle())

    def test_an_idle_ryubing_is_closed_first(self):
        self.log("00:00:00.1 |N| Application ReloadConfig: Loading\n")
        with mock.patch.object(switch, "emulator_pids", side_effect=[[42], []]), \
                mock.patch.object(switch.os, "kill") as kill:
            switch.close_emulator()
        kill.assert_called_once_with(42, switch.signal.SIGTERM)

    def test_a_busy_ryubing_is_left_alone(self):
        self.log(MII)
        with mock.patch.object(switch, "emulator_pids", return_value=[42]), \
                mock.patch.object(switch.os, "kill") as kill:
            with self.assertRaises(OSError):
                switch.close_emulator()
        kill.assert_not_called()

    def test_no_file_no_launch(self):
        self.install_save()
        engine = E.Engine(Config(), Database(self.home / "t.db"))
        self.assertIsNone(launcher.best(engine.db, engine.link_switch(TITLE)))


if __name__ == "__main__":
    unittest.main()
