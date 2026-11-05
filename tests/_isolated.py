"""Points every folder AchTrack uses at a throwaway one. Each test module imports this before AchTrack,
so the tests stay off your real library however they are started (python -m unittest discover -s tests
skips tests/__init__.py)."""
import os
import tempfile

if not os.environ.get("ACHTRACK_TEST_ROOT"):
    _root = tempfile.mkdtemp(prefix="achtrack-tests-")
    os.environ.update(ACHTRACK_TEST_ROOT=_root, XDG_DATA_HOME=f"{_root}/data", XDG_CONFIG_HOME=f"{_root}/config",
                      XDG_CACHE_HOME=f"{_root}/cache", APPDATA=f"{_root}/roaming", LOCALAPPDATA=f"{_root}/local",
                      QT_QPA_PLATFORM="offscreen")
