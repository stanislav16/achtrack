"""Entry point for the macOS app bundle built with PyInstaller."""
import multiprocessing
import sys

from achtrack.__main__ import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main(sys.argv[1:]))
