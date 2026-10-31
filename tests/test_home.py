import tempfile
import time
import unittest
from pathlib import Path

from PySide6.QtWidgets import QApplication

from achtrack import overview
from achtrack.config import Config
from achtrack.db import Database
from achtrack.gui import MainWindow
from achtrack.notifier import Notifier

app = QApplication.instance() or QApplication([])


class Quiet:
    def __init__(self):
        self.asked = []

    def request(self, *args):
        self.asked.append(args)


def fill(db: Database) -> None:
    for appid, name, count, have in ((10, "Nearly Done", 10, 9), (20, "Just Started", 10, 1), (30, "Finished", 5, 5)):
        db.ensure_game(appid)
        db.update_game(appid, name=name, last_played=int(time.time()) - appid)
        db.set_schema(appid, [{"name": f"a{i}", "display_name": f"{name} {i}", "description": "", "icon": "",
                               "icon_gray": "", "hidden": 0, "global_pct": 5.0 + i, "kind": ""}
                              for i in range(count)], {})
        for i in range(have):
            db.add_unlock(appid, f"a{i}", int(time.time()) - i, "Goldberg")


class Overview(unittest.TestCase):
    def setUp(self):
        self.db = Database(Path(tempfile.mkdtemp()) / "a.db")
        fill(self.db)

    def test_figures(self):
        data = overview.collect(self.db)
        self.assertEqual(data["stats"]["unlocked"], 15)
        self.assertEqual(data["stats"]["perfect"], 1)

    def test_almost_there_is_only_games_nearly_done(self):
        names = [g["name"] for g in overview.collect(self.db)["almost"]]
        self.assertEqual(names, ["Nearly Done"])

    def test_hidden_games_stay_out(self):
        self.db.update_game(10, hidden=1)
        data = overview.collect(self.db)
        self.assertFalse([t for t in data["recent"] if t["appid"] == 10])
        self.assertFalse([g for g in data["continue"] if g["appid"] == 10])


class Navigation(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        fill(self.db)
        cfg = Config()
        self.win = MainWindow(cfg, Quiet(), Notifier(cfg), has_tray=False)
        self.win.resize(1240, 800)
        self.win.show()
        self.win.reload(force=True)
        app.processEvents()

    def tearDown(self):
        self.win.hide()

    def test_opens_on_home(self):
        self.assertIs(self.win.stack.currentWidget(), self.win.home)
        self.assertIsNone(self.win.current_appid())
        self.assertTrue(self.win.nav_home.isChecked())

    def test_a_game_and_back(self):
        self.win.open_game(20)
        self.assertIs(self.win.stack.currentWidget(), self.win.detail)
        self.assertEqual(self.win.detail.appid, 20)
        self.assertFalse(self.win.nav_home.isChecked())
        self.win.reload(force=True)            # background updates must not jump back home
        self.assertIs(self.win.stack.currentWidget(), self.win.detail)
        self.win.show_home()
        self.assertIs(self.win.stack.currentWidget(), self.win.home)
        self.win.reload(force=True)            # ...or away from it
        self.assertIs(self.win.stack.currentWidget(), self.win.home)
        self.assertIsNone(self.win.current_appid())

    def test_opening_a_game_the_search_hides(self):
        self.win.search.setText("nothing like this")
        self.win.reload(force=True)
        self.win.open_game(30)
        self.assertEqual(self.win.detail.appid, 30)
        self.assertEqual(self.win.search.text(), "")


if __name__ == "__main__":
    unittest.main()
