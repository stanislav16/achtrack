import os
import tempfile
import time
import unittest
from pathlib import Path

from achtrack import ports, savesync
from achtrack.config import BACKUP_DIR, Config
from achtrack.db import Database


class Layouts(unittest.TestCase):
    def setUp(self):
        self.prefix = tempfile.mkdtemp()
        os.makedirs(f"{self.prefix}/drive_c/users/steamuser/AppData")
        os.makedirs(f"{self.prefix}/drive_c/users/Public")

    def test_a_save_goes_portable_and_back(self):
        layout = savesync.Layout(self.prefix, None)
        real = f"{self.prefix}/drive_c/users/steamuser/Documents/My Games/X/a.sav"
        portable = layout.portable(real)
        self.assertEqual(portable, "user/Documents/My Games/X/a.sav")
        self.assertEqual(layout.real(portable), real)

    def test_paths_that_climb_out_are_refused(self):
        layout = savesync.Layout(self.prefix, None)
        self.assertIsNone(layout.real("user/../../etc/passwd"))
        self.assertIsNone(layout.real("user/./a"))

    def test_a_port_has_its_own_anchor(self):
        layout = savesync.Layout("", None, "/saves/here")
        self.assertEqual(layout.portable("/saves/here/1/CUSA/userdata"), "port/1/CUSA/userdata")
        self.assertEqual(layout.real("port/1/CUSA/userdata"), "/saves/here/1/CUSA/userdata")


class SavesThroughPlay(unittest.TestCase):
    """Two machines sharing a folder, with the port's saves standing in for any game's."""

    def setUp(self):
        self.db = Database(Path(tempfile.mkdtemp()) / "a.db")
        self.cfg = Config()
        self.cfg.sync_dir = tempfile.mkdtemp()
        self.appid = self.db.next_psn_appid()
        self.db.ensure_game(self.appid)
        self.db.update_game(self.appid, name="Bloodborne", psn_id="NPWR")
        self.db.set_meta("ports", {ports.BLOODBORNE.key: self.appid})
        ports.BLOODBORNE.data_dirs[0].mkdir(parents=True, exist_ok=True)
        self.save = ports.BLOODBORNE.saves_dir() / "1" / "CUSA03173" / "SPRJ0005" / "userdata0010"
        self.save.parent.mkdir(parents=True, exist_ok=True)

    def write(self, text: str, age: int) -> None:
        self.save.write_text(text)
        when = time.time() - age
        os.utime(self.save, (when, when))

    def offer(self, device: str = "B"):
        return savesync.newer_elsewhere(self.db, self.cfg, BACKUP_DIR, self.appid, device)

    def test_newer_save_is_offered_once(self):
        self.write("from A", 1000)
        savesync.backup(self.db, self.cfg, BACKUP_DIR, self.appid, ("A", "PC"))
        self.write("old on B", 5000)
        offer = self.offer()
        self.assertIsNotNone(offer)
        self.assertEqual(offer["device_name"], "PC")
        self.assertFalse(offer["blocked"])
        savesync.restore(self.db, self.cfg, BACKUP_DIR, self.appid, offer["path"], ("B", "Mac"))
        self.assertEqual(self.save.read_text(), "from A")
        self.assertIsNone(self.offer(), "a save already brought over was offered again")

    def test_restore_keeps_what_was_there(self):
        self.write("from A", 1000)
        savesync.backup(self.db, self.cfg, BACKUP_DIR, self.appid, ("A", "PC"))
        self.write("old on B", 5000)
        result = savesync.restore(self.db, self.cfg, BACKUP_DIR, self.appid, self.offer()["path"], ("B", "Mac"))
        self.assertTrue(result["safety"])
        self.assertEqual(savesync.read_manifest(Path(result["safety"]))["note"], "before restore")

    def test_own_backups_are_not_offered(self):
        self.write("mine", 1000)
        savesync.backup(self.db, self.cfg, BACKUP_DIR, self.appid, ("B", "Mac"))
        self.write("older", 5000)
        self.assertIsNone(self.offer())

    def test_an_older_save_elsewhere_is_not_offered(self):
        self.write("from A", 5000)
        savesync.backup(self.db, self.cfg, BACKUP_DIR, self.appid, ("A", "PC"))
        self.write("newer on B", 100)
        self.assertIsNone(self.offer())

    def test_a_port_backup_is_filed_under_the_port(self):
        self.write("x", 10)
        result = savesync.backup(self.db, self.cfg, BACKUP_DIR, self.appid, ("A", "PC"))
        self.assertEqual(Path(result["path"]).parent.name, ports.BLOODBORNE.key)


if __name__ == "__main__":
    unittest.main()
