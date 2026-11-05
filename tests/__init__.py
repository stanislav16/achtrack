"""AchTrack's tests: python -m unittest discover -s tests -t .

Everything runs against throwaway folders (tests/_isolated.py), set up before AchTrack is imported, so a
test can never touch your real library.
"""
from . import _isolated  # noqa: F401
