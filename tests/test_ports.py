import os
import tempfile
import time
import unittest
from pathlib import Path

from achtrack import engine as E
from achtrack import ports
from achtrack.config import Config
from achtrack.db import Database

PORT = ports.BLOODBORNE


class Logs(unittest.TestCase):
    def setUp(self):
        self.logs = PORT.data_dirs[0] / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)
        for old in self.logs.glob("*.log"):
            old.unlink()

    def log(self, name: str, text: str) -> str:
        path = self.logs / name
        path.write_text(text)
        return str(path)

    def test_only_trophy_lines_count(self):
        path = self.log("a.log", "Runtime: boot\nRuntime: trophy 5 unlocked\nnot a trophy 6 unlocked\n")
        self.assertEqual(ports.trophies_in(path, PORT), {5})

    def test_a_half_written_line_waits(self):
        path = self.log("b.log", "Runtime: trophy 1 unlocked\nRuntime: troph")
        self.assertEqual(ports.trophies_in(path, PORT), {1})
        with open(path, "a") as f:
            f.write("y 2 unlocked\n")
        self.assertEqual(ports.trophies_in(path, PORT), {1, 2})

    def test_a_replaced_log_is_read_from_the_start(self):
        path = self.log("c.log", "Runtime: trophy 1 unlocked\nRuntime: trophy 2 unlocked\n")
        ports.trophies_in(path, PORT)
        self.log("c.log", "Runtime: trophy 3 unlocked\n")
        self.assertEqual(ports.trophies_in(path, PORT), {3})

    def aged(self, count: int) -> list:
        made = []
        for i in range(count):
            path = self.log(f"2026102{i}_190000.log", "x\n")
            os.utime(path, (time.time() - 86400, time.time() - 86400))
            made.append(path)
        return made

    def test_tidying_keeps_the_newest_three(self):
        made = self.aged(5)
        self.assertEqual(ports.tidy_logs(PORT, set(made), False), made[:2])
        self.assertEqual(PORT.log_files(), made[2:])

    def test_tidying_spares_what_has_not_been_read(self):
        made = self.aged(5)
        self.assertEqual(ports.tidy_logs(PORT, {made[1]}, False), [made[1]])
        self.assertIn(made[0], PORT.log_files())

    def test_tidying_waits_while_the_game_runs(self):
        made = self.aged(5)
        self.assertEqual(ports.tidy_logs(PORT, set(made), True), [])


class Unlocks(unittest.TestCase):
    def setUp(self):
        PORT.data_dirs[0].joinpath("logs").mkdir(parents=True, exist_ok=True)
        for old in PORT.log_files():
            os.remove(old)
        self.db = Database(Path(tempfile.mkdtemp()) / "a.db")
        self.appid = self.db.next_psn_appid()
        self.db.ensure_game(self.appid)
        self.db.update_game(self.appid, psn_id="NPWR", platform="ps4", name="Bloodborne™")
        self.db.set_schema(self.appid, [
            {"name": f"trophy_{i}", "display_name": f"T{i}", "description": "", "icon": "", "icon_gray": "",
             "hidden": 0, "global_pct": None, "kind": kind}
            for i, kind in ((0, "platinum"), (1, "gold"), (2, "gold"))], {})
        self.popups = []
        listener = E.Listener()
        listener.on_unlock = self.popups.append
        self.engine = E.Engine(Config(), self.db, listener)
        self.engine.started_at = 0

    def test_joins_your_playstation_entry(self):
        self.assertEqual(self.engine.link_port(PORT, self.db), "")
        self.assertEqual(self.engine.port_game(PORT), self.appid)
        self.assertIn(PORT.process, self.db.game(self.appid)["exe_name"])

    def test_trophies_unlock_and_the_platinum_follows(self):
        self.engine.link_port(PORT, self.db)
        log = PORT.data_dirs[0] / "logs" / "s.log"
        log.write_text("Runtime: trophy 1 unlocked\n")
        self.engine.discover_ports()
        self.assertEqual(self.db.unlocked_names(self.appid), {"trophy_1"})
        with open(log, "a") as f:
            f.write("Runtime: trophy 2 unlocked\n")
        os.utime(log, (time.time() + 2, time.time() + 2))
        self.engine.poll()
        self.assertEqual(self.db.unlocked_names(self.appid), {"trophy_0", "trophy_1", "trophy_2"})
        self.assertEqual([p["title"] for p in self.popups], ["T1", "T2", "T0"])

    def test_a_log_from_before_starting_unlocks_silently(self):
        self.engine.link_port(PORT, self.db)
        log = PORT.data_dirs[0] / "logs" / "s.log"
        log.write_text("Runtime: trophy 1 unlocked\n")
        self.engine.started_at = time.time() + 60
        self.engine.discover_ports()
        self.assertEqual(self.db.unlocked_names(self.appid), {"trophy_1"})
        self.assertEqual(self.popups, [])


if __name__ == "__main__":
    unittest.main()
