import os
import subprocess
import tempfile
import time
import unittest

from achtrack import playtime


@unittest.skipIf(os.name == "nt", "a process can't turn into another program on Windows")
class Processes(unittest.TestCase):
    def setUp(self):
        self.monitor = playtime.ProcessMonitor()
        self.process = None

    def tearDown(self):
        if self.process:
            self.process.kill()
            self.process.wait()

    def counted(self, exe_map: dict) -> bool:
        self.monitor.scan(exe_map)
        return any(pid == str(self.process.pid) for pid, *_ in self.monitor.seen.get(7, []))

    def test_a_script_that_becomes_the_game_is_noticed(self):
        # The stuck "not playing" with Bloodborne's port: its script exec's into the game, keeping
        # the pid and start time, and the running app went on remembering the script.
        self.process = subprocess.Popen(["/bin/sh", "-c", "/bin/sleep 1; exec /bin/sleep 20"])
        time.sleep(0.3)
        self.assertFalse(self.counted({"sleep": 7}), "the script itself isn't the game")
        time.sleep(1.2)
        self.assertTrue(self.counted({"sleep": 7}))

    def test_a_game_that_has_exited_stops_counting(self):
        self.process = subprocess.Popen(["/bin/sleep", "20"])
        time.sleep(0.2)
        self.assertTrue(self.counted({"sleep": 7}))
        self.process.kill()
        self.process.wait()
        self.assertFalse(self.counted({"sleep": 7}))

    def test_it_can_be_told_you_are_not_playing(self):
        self.process = subprocess.Popen(["/bin/sleep", "20"])
        time.sleep(0.2)
        self.assertTrue(self.counted({"sleep": 7}))
        self.monitor.ignore_matching(7)
        self.assertFalse(self.counted({"sleep": 7}))


class CrackFolders(unittest.TestCase):
    def setUp(self):
        self.monitor = playtime.ProcessMonitor()
        self.game = tempfile.mkdtemp()

    def detect(self, exe: str):
        class Src:
            def mapped(self, pid):
                return []

            def cwd(self, pid):
                return ""
        self.monitor.src = Src()
        return self.monitor._detect("1", playtime._Proc([exe], exe, {}))

    def test_config_beside_the_executable(self):
        with open(os.path.join(self.game, "steam_appid.txt"), "w") as f:
            f.write("1234")
        found = self.detect(os.path.join(self.game, "Game.exe") if os.name == "nt"
                            else "Z:" + os.path.join(self.game, "Game.exe").replace("/", "\\"))
        self.assertEqual(found[0], 1234)

    def test_config_beside_an_unreal_engine_library(self):
        libs = os.path.join(self.game, "Engine", "Binaries", "ThirdParty", "Steamworks", "Steamv151", "Win64")
        os.makedirs(libs)
        with open(os.path.join(libs, "steam_appid.txt"), "w") as f:
            f.write("5678")
        exe = os.path.join(self.game, "Sack", "Binaries", "Win64", "Sack-Win64-Shipping.exe")
        os.makedirs(os.path.dirname(exe))
        found = self.detect(exe if os.name == "nt" else "Z:" + exe.replace("/", "\\"))
        self.assertEqual(found[0], 5678)

    def test_a_steam_install_is_left_alone(self):
        self.assertTrue(playtime._is_steam_install("/x/steamapps/common/Game/g.exe"))
        self.assertTrue(playtime._is_steam_install(r"C:\Steam\steamapps\common\Game\g.exe"))


if __name__ == "__main__":
    unittest.main()
