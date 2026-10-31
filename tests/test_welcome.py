import unittest
from unittest import mock

from PySide6.QtWidgets import QApplication

from achtrack.config import Config
from achtrack.gui import MainWindow, WelcomeDialog
from achtrack.notifier import Notifier

app = QApplication.instance() or QApplication([])


class Quiet:
    def __init__(self):
        self.asked = []

    def request(self, *args):
        self.asked.append(args)


class Welcome(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.cfg.save = mock.Mock()     # the real one would write the test profile's settings
        self.worker = Quiet()
        self.win = MainWindow(self.cfg, self.worker, Notifier(self.cfg), has_tray=False)
        # Never the real one: on a Mac it adds or removes the actual login item.
        patcher = mock.patch("achtrack.desktop.set_autostart")
        self.autostart = patcher.start()
        self.addCleanup(patcher.stop)

    def walk_to_done(self, dialog: WelcomeDialog) -> None:
        while dialog.next.text() != "Done":
            dialog.next.click()
        dialog.next.click()

    def test_keys_are_saved_and_put_to_use(self):
        dialog = WelcomeDialog(self.win)
        dialog.key.setText("  the key  ")
        dialog.psn.setText("the token")
        self.walk_to_done(dialog)
        self.assertEqual(self.cfg.steam_api_key, "the key")
        self.assertEqual(self.cfg.psn_npsso, "the token")
        self.assertTrue(self.cfg.welcomed)
        self.assertIn(("refresh_all",), self.worker.asked)
        self.assertIn(("psn_sync",), self.worker.asked)
        self.autostart.assert_called_once_with(True)

    def test_skipping_everything_asks_for_nothing(self):
        dialog = WelcomeDialog(self.win)
        dialog.autostart.setChecked(False)
        self.walk_to_done(dialog)
        self.assertEqual(self.cfg.steam_api_key, "")
        self.assertEqual(self.worker.asked, [])
        self.autostart.assert_called_once_with(False)

    def test_the_button_says_skip_until_something_is_typed(self):
        dialog = WelcomeDialog(self.win)
        dialog.next.click()
        self.assertEqual(dialog.next.text(), "Skip")
        dialog.key.setText("x")
        self.assertEqual(dialog.next.text(), "Continue")

    def test_closing_it_counts_as_seen(self):
        dialog = WelcomeDialog(self.win)
        dialog.reject()
        self.assertTrue(self.cfg.welcomed)
        self.autostart.assert_not_called()

    def test_someone_already_set_up_is_not_welcomed(self):
        self.cfg.steam_api_key = "already"
        self.win._welcome()
        self.assertTrue(self.cfg.welcomed)
        self.assertIsNone(getattr(self.win, "_welcome_dialog", None))


if __name__ == "__main__":
    unittest.main()
