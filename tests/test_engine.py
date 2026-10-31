import tempfile
import time
import unittest
from pathlib import Path

from achtrack import engine as E
from achtrack.config import Config
from achtrack.db import Database


class Sessions(unittest.TestCase):
    """The stuck "playing now": something in the stop path failed, and the session never closed."""

    def setUp(self):
        self.db = Database(Path(tempfile.mkdtemp()) / "a.db")
        self.db.ensure_game(42)     # with its details filled in, so nothing goes looking on the network
        self.db.update_game(42, name="Game", genre="x", header_url="x", schema_fetched_at=int(time.time()))
        self.stopped = []
        listener = E.Listener()
        listener.on_game_stopped = lambda appid, seconds: self.stopped.append(appid)
        cfg = Config()
        cfg.save_detection = False
        cfg.export_auto = False
        cfg.saves_auto_backup = False
        self.engine = E.Engine(cfg, self.db, listener)
        self.found = {42: None}

        class Monitor:
            prefixes, seen = {}, {}

            def scan(_self, exe_map):
                return dict(self.found)

            def ignore_matching(_self, appid):
                return 1

        self.engine.monitor = Monitor()

    def test_a_game_starts_and_stops(self):
        self.engine.track_processes()
        self.assertIn(42, self.engine.running)
        self.assertEqual(self.db.open_sessions(), {42})
        self.found = {}
        self.engine.track_processes()       # launchers hand off, so one empty scan isn't the end
        self.assertIn(42, self.engine.running)
        self.engine.track_processes()
        self.assertNotIn(42, self.engine.running)
        self.assertEqual(self.stopped, [42])
        self.assertEqual(self.db.open_sessions(), set())

    def test_ending_a_session_by_hand(self):
        self.engine.track_processes()
        self.engine.end_session(42)
        self.assertNotIn(42, self.engine.running)
        self.assertEqual(self.db.open_sessions(), set())

    def test_play_time_is_counted(self):
        self.engine.track_processes()
        self.engine.running[42].last = time.time() - 2
        self.engine.track_processes()
        self.assertGreater(self.engine.running[42].seconds, 1)


class EmulatorFiles(unittest.TestCase):
    def test_a_new_unlock_pops_up_and_an_old_one_does_not(self):
        db = Database(Path(tempfile.mkdtemp()) / "a.db")
        db.ensure_game(7)
        db.update_game(7, name="Game", schema_fetched_at=int(time.time()), genre="x", header_url="x")
        popups = []
        listener = E.Listener()
        listener.on_unlock = popups.append
        engine = E.Engine(Config(), db, listener)
        path = Path(tempfile.mkdtemp()) / "achievements.json"
        path.write_text('{"OLD": {"earned": true, "earned_time": 1700000000}}')
        engine.started_at = time.time() + 60            # written before AchTrack started
        engine.sources[str(path)] = {"path": str(path), "appid": 7, "emulator": "Goldberg", "mtime": 0.0, "size": -1}
        engine._process(str(path))
        self.assertEqual(db.unlocked_names(7), {"OLD"})
        self.assertEqual(popups, [])
        engine.started_at = 0
        path.write_text('{"OLD": {"earned": true, "earned_time": 1700000000},'
                        ' "NEW": {"earned": true, "earned_time": 1700000100}}')
        engine._process(str(path))
        self.assertEqual(db.unlocked_names(7), {"OLD", "NEW"})
        self.assertEqual(len(popups), 1)


if __name__ == "__main__":
    unittest.main()
