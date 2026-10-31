import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from achtrack import launcher, scanner
from achtrack.db import Database


class Choosing(unittest.TestCase):
    def setUp(self):
        self.db = Database(Path(tempfile.mkdtemp()) / "a.db")
        self.db.ensure_game(100)
        self.db.update_game(100, name="Some Game")

    def test_your_choice_wins(self):
        self.db.update_game(100, steam_owned=1, launch="program:/x/run.sh")
        self.assertEqual(launcher.best(self.db, 100).stored(), "program:/x/run.sh")

    def test_a_steam_game(self):
        self.db.update_game(100, steam_owned=1)
        with mock.patch.object(launcher, "steam_installed", return_value=True):
            self.assertEqual(launcher.best(self.db, 100).kind, "steam")

    def test_nothing_known(self):
        with mock.patch.object(scanner, "heroic_installed", return_value=[]), \
                mock.patch.object(launcher, "steam_installed", return_value=False), \
                mock.patch.object(launcher, "crossover_app", return_value=None):
            self.assertIsNone(launcher.best(self.db, 100))

    def test_a_heroic_game_matches_despite_punctuation(self):
        self.db.update_game(100, name="Bloodborne™")
        entry = [{"title": "Bloodborne", "app_name": "43Vr", "store": "sideload", "folder": "", "exe": ""}]
        with mock.patch.object(scanner, "heroic_installed", return_value=entry), \
                mock.patch.object(launcher, "steam_installed", return_value=False):
            self.assertEqual(launcher.best(self.db, 100).stored(), "heroic:sideload/43Vr")


class Heroic(unittest.TestCase):
    def test_a_sideloaded_game_keeps_its_app_name(self):
        base = Path(tempfile.mkdtemp())
        (base / "sideload_apps").mkdir()
        (base / "sideload_apps" / "library.json").write_text(json.dumps(
            {"games": [{"app_name": "43Vr", "title": "Bloodborne", "install": {"executable": "/g/play.sh"}}]}))
        with mock.patch.object(scanner, "heroic_config_dirs", return_value=[base]):
            found = scanner.heroic_installed()
        self.assertEqual((found[0]["title"], found[0]["app_name"], found[0]["store"]),
                         ("Bloodborne", "43Vr", "sideload"))


class Starting(unittest.TestCase):
    def test_links(self):
        with mock.patch.object(launcher, "_open") as opened:
            launcher.start(launcher.Launch("steam", "730", "Steam"), 730)
            opened.assert_called_with("steam://rungameid/730")
            launcher.start(launcher.Launch("heroic", "sideload/43 Vr", "Heroic"), -1)
            opened.assert_called_with("heroic://launch?appName=43+Vr&runner=sideload")

    def test_a_missing_program_is_an_error(self):
        with self.assertRaises(OSError):
            launcher.start(launcher.Launch("program", "/nowhere/game", "game"), 1)

    @unittest.skipIf(os.name == "nt", "uses a shell script")
    def test_a_program_is_told_which_game_it_is(self):
        folder = tempfile.mkdtemp()
        script = os.path.join(folder, "run.sh")
        with open(script, "w") as f:
            f.write(f'#!/bin/sh\necho "$ACHTRACK_APPID" > "{folder}/out"\n')
        os.chmod(script, 0o755)
        launcher.start(launcher.Launch("program", script, "run.sh"), 55)
        out = Path(folder) / "out"
        for _ in range(50):
            if out.exists() and out.read_text().strip():
                break
            import time
            time.sleep(0.1)
        self.assertEqual(out.read_text().strip(), "55")


if __name__ == "__main__":
    unittest.main()
