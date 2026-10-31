import json
import tempfile
import unittest
from pathlib import Path

from achtrack.emulators import parse_file


class Files(unittest.TestCase):
    def write(self, name: str, text: str) -> Path:
        path = Path(tempfile.mkdtemp()) / name
        path.write_text(text)
        return path

    def test_goldberg(self):
        states = parse_file(self.write("achievements.json", json.dumps(
            {"ONE": {"earned": True, "earned_time": 1700000000}, "TWO": {"earned": False, "earned_time": 0}})))
        self.assertTrue(states["ONE"].unlocked)
        self.assertEqual(states["ONE"].unlocked_at, 1700000000)
        self.assertFalse(states["TWO"].unlocked)

    def test_codex_and_rune(self):
        states = parse_file(self.write("achievements.ini", "[ONE]\nAchieved=1\nUnlockTime=1700000000\n"
                                                           "[TWO]\nAchieved=0\nCurProgress=3\nMaxProgress=10\n"))
        self.assertTrue(states["ONE"].unlocked)
        self.assertEqual(states["ONE"].unlocked_at, 1700000000)
        self.assertFalse(states["TWO"].unlocked)

    def test_rubbish_is_not_a_crash(self):
        path = self.write("achievements.json", "{ not json")
        try:
            self.assertEqual(parse_file(path), {})
        except ValueError:
            pass        # the engine treats this as "mid-write, try again"


if __name__ == "__main__":
    unittest.main()
