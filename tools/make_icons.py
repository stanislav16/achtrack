"""Renders the app icon from the SVG: .icns for macOS, PNGs for Linux, .ico for Windows.

Run through the installed virtualenv (it uses Qt's SVG renderer):
    ~/.local/share/achtrack/venv/bin/python tools/make_icons.py [outdir]
"""
from __future__ import annotations

import json
import shutil
import struct
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

ROOT = Path(__file__).resolve().parent.parent
SVG = ROOT / "achtrack" / "assets" / "achtrack.svg"
GLYPH = ROOT / "achtrack" / "assets" / "achtrack-glyph.svg"     # the trophy with no tile behind it
# macOS 26 and later draw the tile, the material and the shading themselves from a layered
# ".icon" document; all we hand over is the artwork and the colour behind it.
TILE_COLOUR = "extended-srgb:0.11765,0.11765,0.12549,1.00000"
# macOS wants each size as 1x and 2x inside an .iconset folder.
ICNS_SIZES = (16, 32, 128, 256, 512)
PNG_SIZES = (16, 32, 48, 64, 128, 256, 512)
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


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


def make_ico(out_dir: Path) -> Path:
    """Windows draws taskbar and title-bar icons from this; AchTrack ships it in assets/."""
    pngs = []
    for size in ICO_SIZES:
        dest = out_dir / f"ico-{size}.png"
        render(SVG, size, dest)
        pngs.append(dest.read_bytes())
        dest.unlink()
    offset = 6 + 16 * len(pngs)
    head = struct.pack("<HHH", 0, 1, len(pngs))
    for size, png in zip(ICO_SIZES, pngs):
        head += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
    ico = out_dir / "achtrack.ico"
    ico.write_bytes(head + b"".join(pngs))
    return ico


def make_icon_document(out_dir: Path) -> Path:
    """Writes AchTrack.icon - the source Xcode's actool compiles into a layered icon."""
    document = out_dir / "AchTrack.icon"
    render(GLYPH, 1024, document / "Assets" / "trophy.png")
    (document / "icon.json").write_text(json.dumps({
        "fill": {"automatic-gradient": TILE_COLOUR},
        "groups": [{"layers": [{"image-name": "trophy.png", "name": "Trophy"}]}],
        "supported-platforms": {"squares": ["macOS"]},
    }, indent=2) + "\n", encoding="utf-8")
    return document


def main() -> int:
    """make_icons.py [outdir] [--icns] [--pngs] [--icon] [--ico]; without flags, whatever this system uses."""
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
    if "--ico" in flags:
        print(f"ico: {make_ico(out_dir)}")
    if "--icon" in flags or (not flags and sys.platform == "darwin"):
        print(f"icon: {make_icon_document(out_dir)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
