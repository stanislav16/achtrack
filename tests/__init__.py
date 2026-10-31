"""AchTrack's tests: python -m unittest discover -s tests -t .

Everything runs against throwaway folders, set up here before AchTrack is imported, so a test can
never touch your real library.
"""
import os
import tempfile

_root = tempfile.mkdtemp(prefix="achtrack-tests-")
os.environ.update(XDG_DATA_HOME=f"{_root}/data", XDG_CONFIG_HOME=f"{_root}/config", XDG_CACHE_HOME=f"{_root}/cache",
                  APPDATA=f"{_root}/roaming", LOCALAPPDATA=f"{_root}/local", QT_QPA_PLATFORM="offscreen")
