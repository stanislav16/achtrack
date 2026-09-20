"""Renders the app icon from the SVG: .icns for macOS, PNGs for Linux.

Run through the installed virtualenv (it uses Qt's SVG renderer):
    ~/.local/share/achtrack/venv/bin/python tools/make_icons.py [outdir]
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

ROOT = Path(__file__).resolve().parent.parent
SVG = ROOT / "achtrack" / "assets" / "achtrack.svg"
# macOS wants each size as 1x and 2x inside an .iconset folder.
ICNS_SIZES = (16, 32, 128, 256, 512)
PNG_SIZES = (16, 32, 48, 64, 128, 256, 512)


def render(svg: Path, size: int, dest: Path) -> None:
    image = QImage(QSize(size, size), QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
    QSvgRenderer(str(svg)).render(painter)
    painter.end()
    dest.parent.mkdir(parents=True, exist_ok=True)
    image.save(str(dest))


def make_icns(out_dir: Path) -> Path | None:
    iconset = out_dir / "AchTrack.iconset"
    for size in ICNS_SIZES:
        render(SVG, size, iconset / f"icon_{size}x{size}.png")
        render(SVG, size * 2, iconset / f"icon_{size}x{size}@2x.png")
    icns = out_dir / "AchTrack.icns"
    try:
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)], check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError) as e:
        print(f"iconutil failed: {e}")
        return None
    finally:
        shutil.rmtree(iconset, ignore_errors=True)      # only the .icns is needed
    return icns


def make_pngs(out_dir: Path) -> list:
    made = []
    for size in PNG_SIZES:
        dest = out_dir / f"achtrack-{size}.png"
        render(SVG, size, dest)
        made.append(dest)
    return made


def main() -> int:
    """make_icons.py [outdir] [--icns] [--pngs]; without flags, whatever this system uses."""
    QGuiApplication(["make_icons", "-platform", "offscreen"])
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    out_dir = Path(args[0]).expanduser() if args else ROOT / "build"
    out_dir.mkdir(parents=True, exist_ok=True)
    want_icns = "--icns" in flags or (not flags and sys.platform == "darwin")
    want_pngs = "--pngs" in flags or (not flags and sys.platform != "darwin")
    if want_icns:
        icns = make_icns(out_dir)
        print(f"icns: {icns}" if icns else "icns: failed")
    if want_pngs:
        made = make_pngs(out_dir)
        print(f"pngs: {len(made)} sizes in {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
