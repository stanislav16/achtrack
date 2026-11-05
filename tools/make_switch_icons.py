"""Draws the achievement icons for AchTrack's own Switch goals (Mario Kart 8 Deluxe, Mario Kart World and Donkey Kong Bananza).

Each icon is a line glyph for what the goal is about, in AchTrack's own look: one even line weight, near-white on a
graphite tile, like the app's trophy. How hard the goal is (bronze, silver, gold, platinum) shows only as a faint
wash behind the glyph. The app rounds the corners itself. They are AchTrack's own drawings, not the games' artwork. Run from the repository root:

    python tools/make_switch_icons.py

It writes achtrack/assets/switch/<title key>/<goal>.png (256 px). Needs Pillow.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from achtrack import switch  # noqa: E402

S = 1024                      # drawn large, then scaled down for smooth edges
OUT = 256
W = 46                        # line weight, the same for every glyph (the app's own trophy: 3 px in 64)
INK = (245, 245, 247, 255)    # theme "text"
DIM = (245, 245, 247, 120)
CLEAR = (0, 0, 0, 0)
TOP, BOTTOM = (46, 46, 50), (28, 28, 30)   # graphite, like the app's cards
GLOW = {   # a faint wash behind the glyph, the only hint of how hard the goal is
    "b": (205, 140, 95, 34), "s": (200, 206, 216, 30), "g": (232, 192, 100, 42), "p": (186, 196, 236, 46),
}

# goal -> (glyph, tier)
GOALS = {
    # Mario Kart World
    "race_1": ("flag", "b"), "race_25": ("flag", "s"), "race_100": ("flag", "g"), "race_500": ("flag", "p"),
    "win_1": ("crown", "b"), "win_10": ("crown", "s"), "win_50": ("crown", "g"), "win_200": ("crown", "p"),
    "podium_25": ("podium", "s"), "comeback": ("rocket", "g"),
    "overtake_500": ("chevrons", "s"), "overtake_5000": ("chevrons", "g"),
    "cup_1": ("cup", "b"), "cup_25": ("cup", "s"), "gold_1": ("cup1", "g"), "gold_8": ("cup1", "p"), "perfect": ("stars3", "g"),
    "course_5": ("pin", "b"), "course_15": ("pin", "s"), "course_30": ("globe", "p"),
    "driver_5": ("wheel", "b"), "driver_20": ("wheel", "g"),
    "vehicle_5": ("kart", "b"), "vehicle_20": ("kart", "g"),
    "coin_20": ("coin", "b"), "coin_1000": ("coins", "s"), "coin_10000": ("coins", "g"),
    "time_trial_1": ("watch", "b"), "time_trial_25": ("watch", "g"),
    "free_roam": ("road", "b"), "mission_1": ("pswitch", "b"), "mission_25": ("pswitch", "s"),
    "clear_1": ("pswitch_check", "b"), "clear_25": ("pswitch_check", "s"), "clear_100": ("pswitch_check", "g"),
    "clear_all": ("pswitch_check", "p"),
    "panel_1": ("panel", "b"), "panel_25": ("panel", "s"), "panel_75": ("panel", "g"), "panel_all": ("panel", "p"),
    "medal_1": ("medal", "b"), "medal_25": ("medal", "s"), "medal_100": ("medal", "g"), "medal_all": ("medal", "p"),
    # Mario Kart 8 Deluxe
    "race_50": ("flag", "s"), "race_250": ("flag", "g"), "race_1000": ("flag", "p"),
    "trophy_1": ("cup", "b"), "trophy_12": ("cup", "s"), "trophy_30": ("cup", "g"), "trophy_60": ("cup", "p"),
    "ending": ("film", "s"), "gold_mario": ("crown", "g"),
    "ghost_150": ("ghost", "s"), "ghost_150_all": ("ghost", "g"), "ghost_200": ("ghost_bolt", "g"),
    "ghost_200_all": ("ghost_bolt", "p"),
    "parts_30": ("gear", "s"), "parts_50": ("gear", "g"), "mii_suit": ("shirt", "s"),
    "ultra_100": ("spark", "g"), "drift_5000": ("drift", "s"), "trick_1000": ("star", "s"),
    "lakitu_50": ("cloud", "b"), "fly_30": ("wing", "s"), "swim_30": ("wave", "s"),
    "battle_1": ("balloon", "b"), "battle_all": ("balloon", "g"),
    # Donkey Kong Bananza
    "dk_banana_1": ("banana", "b"), "dk_banana_50": ("banana", "s"), "dk_banana_150": ("bananas", "s"),
    "dk_banana_300": ("bananas", "g"), "dk_banana_500": ("bananas", "g"), "dk_banana_all": ("bananas", "p"),
    "dk_fossil_1": ("bone", "b"), "dk_fossil_50": ("bone", "s"), "dk_fossil_200": ("bone", "s"), "dk_fossil_400": ("bone", "g"),
    "dk_fossil_all": ("bone", "p"),
    "dk_chest_1": ("chest", "b"), "dk_chest_25": ("chest", "s"), "dk_chest_all": ("chest", "g"),
    "dk_skill_1": ("fist", "b"), "dk_skill_15": ("fist", "s"), "dk_skill_all": ("fist", "g"),
    "dk_layer_3": ("layers", "b"), "dk_layer_9": ("layers", "s"), "dk_layer_all": ("layers", "g"), "dk_core": ("core", "g"),
    "dk_clear": ("film", "g"), "dk_island": ("palm", "s"), "dk_emerald_rush": ("emerald", "p"),
}


def font(size: int, bold: bool = True):
    for name in ("/System/Library/Fonts/SFNS.ttf", "/System/Library/Fonts/Helvetica.ttc", "DejaVuSans-Bold.ttf"):
        try:
            f = ImageFont.truetype(name, size)
            try:
                f.set_variation_by_name("Semibold" if bold else "Regular")
            except Exception:
                pass
            return f
        except OSError:
            continue
    return ImageFont.load_default()


# --- drawing helpers: every stroke W wide with round ends and joins ------------------------------------------------

def dot(d, x, y, r, fill=INK):
    d.ellipse((x - r, y - r, x + r, y + r), fill=fill)


def line(d, pts, w=W, fill=INK, closed=False):
    pts = [tuple(p) for p in pts]
    if closed:
        pts = pts + [pts[0], pts[1]]
    d.line(pts, fill=fill, width=w, joint="curve")
    if not closed:
        for x, y in (pts[0], pts[-1]):
            dot(d, x, y, w / 2, fill)
    for x, y in pts:   # PIL's curve joints leave notches on sharp turns: a round cap on every vertex closes them
        dot(d, x, y, w / 2 - 1, fill)


def ring(d, cx, cy, r, w=W, fill=INK):   # a circle whose line is centred on radius r
    d.ellipse((cx - r - w / 2, cy - r - w / 2, cx + r + w / 2, cy + r + w / 2), outline=fill, width=w)


def box(d, x0, y0, x1, y1, radius, w=W, fill=INK):   # a rounded rectangle, line centred on its edge
    h = w / 2
    d.rounded_rectangle((x0 - h, y0 - h, x1 + h, y1 + h), radius=radius + h, outline=fill, width=w)


def erase_box(d, x0, y0, x1, y1, radius):   # clear the inside of a shape that sits in front of another
    h = W / 2
    d.rounded_rectangle((x0 - h, y0 - h, x1 + h, y1 + h), radius=radius + h, fill=CLEAR)


def erase_circle(d, cx, cy, r):
    r += W / 2
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=CLEAR)


def arc(d, cx, cy, r, a0, a1, w=W, fill=INK, ry=None):   # degrees, clockwise from 3 o'clock, round ends
    ry = r if ry is None else ry
    pts = [(cx + r * math.cos(math.radians(a)), cy + ry * math.sin(math.radians(a)))
           for a in [a0 + (a1 - a0) * i / 64 for i in range(65)]]
    line(d, pts, w, fill)


def outline_of(d, mask: Image.Image, fill=INK, w=W):
    """The edge of a filled shape (an "L" mask), drawn W wide just inside it."""
    inner = mask.filter(ImageFilter.GaussianBlur(w / 2.37)).point(lambda v: 255 if v >= 254 else 0)   # eats w deep
    edge = ImageChops.subtract(mask, inner)
    d.bitmap((0, 0), edge, fill=fill)


def mask():
    m = Image.new("L", (S, S), 0)
    return m, ImageDraw.Draw(m)


def text(d, xy, s: str, size: int, fill=INK):
    f = font(size)
    b = d.textbbox((0, 0), s, font=f)
    d.text((xy[0] - (b[0] + b[2]) / 2, xy[1] - (b[1] + b[3]) / 2), s, font=f, fill=fill)


def star_pts(cx, cy, r, inner=0.48, points=5, turn=0.0):
    return [(cx + (r if i % 2 == 0 else r * inner) * math.sin(i * math.pi / points + turn),
             cy - (r if i % 2 == 0 else r * inner) * math.cos(i * math.pi / points + turn)) for i in range(points * 2)]


def star(d, cx, cy, r, w=W, fill=INK, inner=0.48, points=5):
    line(d, star_pts(cx, cy, r, inner, points), w, fill, closed=True)


def bezier(p0, p1, p2, p3, n=32):
    out = []
    for i in range(n + 1):
        t = i / n; u = 1 - t
        out.append((u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0],
                    u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1]))
    return out


# --- glyphs, on a 1024 grid, kept inside about 250..774 --------------------------------------------------------------

def g_flag(d):   # a chequered flag on a pole
    line(d, [(330, 790), (330, 250)])
    x0, y0, x1, y1 = 330, 260, 750, 540
    line(d, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)], closed=True)
    cw, ch = (x1 - x0) / 4, (y1 - y0) / 3
    for r in range(3):
        for c in range(4):
            if (r + c) % 2 == 0:
                d.rectangle((x0 + c * cw, y0 + r * ch, x0 + (c + 1) * cw, y0 + (r + 1) * ch), fill=INK)


def g_crown(d):
    line(d, [(290, 700), (262, 380), (402, 520), (512, 320), (622, 520), (762, 380), (734, 700)], closed=True)
    line(d, [(300, 790), (724, 790)])


def _cup(d):
    bowl = [(352, 270)] + bezier((352, 270), (352, 470), (420, 560), (512, 560))[1:] + \
        bezier((512, 560), (604, 560), (672, 470), (672, 270))[1:]
    line(d, bowl, closed=True)
    arc(d, 352, 360, 90, 90, 270, ry=70)
    arc(d, 672, 360, 90, -90, 90, ry=70)
    line(d, [(512, 560), (512, 680)])
    line(d, [(400, 770), (412, 690), (612, 690), (624, 770)], closed=True)


def g_cup(d):
    _cup(d)


def g_cup1(d):
    _cup(d)
    text(d, (512, 400), "1", 190)


def g_podium(d):
    line(d, [(250, 790), (250, 580), (412, 580), (412, 450), (612, 450), (612, 640), (774, 640), (774, 790)], closed=True)
    star(d, 512, 290, 100, w=int(W * 0.85))


def g_rocket(d):   # a comeback: the line climbs from the back
    line(d, [(262, 720), (420, 560), (540, 650), (742, 380)])
    line(d, [(612, 370), (750, 370), (750, 508)])


def g_chevrons(d):
    for y, f in ((320, INK), (500, INK), (680, DIM)):
        line(d, [(320, y + 110), (512, y - 50), (704, y + 110)], fill=f)


def g_stars3(d):
    star(d, 512, 360, 170)
    star(d, 312, 660, 110, w=int(W * 0.85))
    star(d, 712, 660, 110, w=int(W * 0.85))


def g_pin(d):
    cx, cy, r, tip = 512, 440, 220, (512, 800)
    dist = tip[1] - cy
    a = math.degrees(math.acos(r / dist))          # where the sides leave the circle
    pts = [tip] + [(cx + r * math.cos(math.radians(t)), cy + r * math.sin(math.radians(t)))
                   for t in [90 - a + (-(180 + 2 * (90 - a))) * i / 64 for i in range(65)]]
    line(d, pts, closed=True)
    ring(d, cx, cy, 70)


def g_globe(d):
    ring(d, 512, 512, 262)
    d.ellipse((512 - 120 - W / 2, 250 - W / 2, 512 + 120 + W / 2, 774 + W / 2), outline=INK, width=W)
    line(d, [(250, 512), (774, 512)])
    arc(d, 512, 512, 230, 200, 340, w=int(W * 0.8), ry=150)
    arc(d, 512, 512, 230, 20, 160, w=int(W * 0.8), ry=150)


def g_wheel(d):   # a steering wheel
    ring(d, 512, 512, 262)
    ring(d, 512, 512, 62)
    for a in (180, 0, 90):
        r = math.radians(a)
        line(d, [(512 + 62 * math.cos(r), 512 + 62 * math.sin(r)), (512 + 262 * math.cos(r), 512 + 262 * math.sin(r))])


def g_kart(d):
    line(d, [(232, 610), (252, 520), (420, 520), (470, 410), (600, 410), (650, 520), (792, 540), (792, 610)], closed=True)
    line(d, [(600, 410), (640, 330)])
    for x in (360, 676):
        erase_circle(d, x, 640, 82)
        ring(d, x, 640, 82)
        dot(d, x, 640, 22)


def g_coin(d):
    ring(d, 512, 512, 262)
    box(d, 476, 372, 548, 652, 36)


def g_coins(d):   # a stack of three, seen a little from above
    for i, cy in enumerate((690, 560, 430)):
        rx, ry, thick = 230, 80, 60
        d.rectangle((512 - rx - W / 2, cy, 512 + rx + W / 2, cy + thick), fill=CLEAR)
        d.ellipse((512 - rx - W / 2, cy + thick - ry - W / 2, 512 + rx + W / 2, cy + thick + ry + W / 2), fill=CLEAR)
        d.ellipse((512 - rx - W / 2, cy - ry - W / 2, 512 + rx + W / 2, cy + ry + W / 2), fill=CLEAR)
        arc(d, 512, cy + thick, rx, 0, 180, ry=ry)
        line(d, [(512 - rx, cy), (512 - rx, cy + thick)])
        line(d, [(512 + rx, cy), (512 + rx, cy + thick)])
        d.ellipse((512 - rx - W / 2, cy - ry - W / 2, 512 + rx + W / 2, cy + ry + W / 2), outline=INK, width=W)
    box(d, 492, 404, 532, 456, 18, w=int(W * 0.7))


def g_watch(d):   # a stopwatch
    ring(d, 512, 560, 232)
    line(d, [(512, 328), (512, 260)])
    line(d, [(452, 250), (572, 250)])
    line(d, [(690, 370), (726, 334)])
    line(d, [(512, 560), (512, 430)])
    dot(d, 512, 560, 34)


def g_road(d):   # off the map: an open road
    line(d, [(436, 250), (262, 790)])
    line(d, [(588, 250), (762, 790)])
    for y0, y1 in ((300, 370), (460, 560), (650, 780)):
        line(d, [(512, y0), (512, y1)], w=int(W * 0.8))


def _pswitch(d):
    box(d, 336, 330, 688, 660, 130)
    erase_box(d, 266, 640, 758, 770, 34)
    box(d, 266, 640, 758, 770, 34)
    text(d, (512, 488), "P", 240)


def g_pswitch(d):
    _pswitch(d)


def g_pswitch_check(d):
    _pswitch(d)
    erase_circle(d, 724, 300, 96)
    ring(d, 724, 300, 96)
    line(d, [(682, 302), (714, 336), (770, 266)], w=int(W * 0.85))


def g_panel(d):   # a ? Panel
    box(d, 262, 262, 762, 762, 64)
    for x, y in ((322, 322), (702, 322), (322, 702), (702, 702)):
        dot(d, x, y, 20)
    text(d, (512, 520), "?", 330)


def g_medal(d):
    line(d, [(392, 250), (472, 470)])
    line(d, [(632, 250), (552, 470)])
    erase_circle(d, 512, 640, 168)
    ring(d, 512, 640, 168)
    star(d, 512, 646, 82, w=int(W * 0.8))


def g_film(d):   # the ending
    box(d, 252, 310, 772, 714, 44)
    for y in (380, 644):
        for x in range(312, 732, 84):
            d.rounded_rectangle((x, y - 18, x + 40, y + 18), radius=10, fill=INK)
    line(d, [(462, 440), (462, 584), (584, 512)], w=int(W * 0.85), closed=True)


def _ghost(d):
    top = [(512 + 212 * math.cos(math.radians(a)), 470 + 212 * math.sin(math.radians(a))) for a in range(180, 361, 6)]
    hem = [(724, 770), (653, 710), (582, 770), (512, 710), (442, 770), (371, 710), (300, 770)]
    line(d, top + hem, closed=True)
    for x in (442, 582):
        d.ellipse((x - 30, 430, x + 30, 510), fill=INK)


def g_ghost(d):
    _ghost(d)


def g_ghost_bolt(d):
    _ghost(d)
    pts = [(760, 170), (668, 340), (736, 340), (690, 480), (820, 290), (750, 290), (806, 170)]
    d.polygon(pts, fill=CLEAR)
    m, md = mask(); md.polygon(pts, fill=255)
    d.bitmap((0, 0), m.filter(ImageFilter.MaxFilter(25)), fill=CLEAR)
    line(d, pts, w=int(W * 0.75), closed=True)


def g_gear(d):
    pts = []
    for i in range(8):
        base = i * 45
        for a, r in ((-14, 220), (-9, 268), (9, 268), (14, 220)):
            t = math.radians(base + a)
            pts.append((512 + r * math.cos(t), 512 + r * math.sin(t)))
    line(d, pts, closed=True)
    ring(d, 512, 512, 84)


def g_shirt(d):
    line(d, [(400, 260), (300, 290), (200, 420), (300, 480), (346, 432), (346, 780), (678, 780), (678, 432),
             (724, 480), (824, 420), (724, 290), (624, 260), (590, 320), (434, 320)], closed=True)


def g_spark(d):   # an ultra mini-turbo: a four-pointed spark
    line(d, star_pts(492, 532, 280, inner=0.22, points=4), closed=True)
    line(d, star_pts(746, 278, 86, inner=0.25, points=4), w=int(W * 0.75), closed=True)


def g_drift(d):   # a long slide round a bend
    end = 410
    arc(d, 512, 512, 250, 150, end)
    t = math.radians(end)
    ex, ey = 512 + 250 * math.cos(t), 512 + 250 * math.sin(t)
    tx, ty = -math.sin(t), math.cos(t)                 # the way the arc runs at its end
    head = []
    for side in (-1, 1):
        a = math.atan2(ty, tx) + math.pi + side * math.radians(40)
        head.append((ex + 120 * math.cos(a), ey + 120 * math.sin(a)))
    line(d, [head[0], (ex, ey), head[1]])
    arc(d, 512, 512, 140, 170, 330, w=int(W * 0.8), fill=DIM)


def g_star(d):
    star(d, 512, 540, 300)


def g_cloud(d):   # Lakitu's cloud
    m, md = mask()
    for x, y, r in ((380, 560, 140), (530, 470, 180), (670, 570, 130)):
        md.ellipse((x - r, y - r, x + r, y + r), fill=255)
    md.rounded_rectangle((240, 560, 800, 700), radius=70, fill=255)
    outline_of(d, m)
    for x in (470, 590):
        d.ellipse((x - 22, 520, x + 22, 584), fill=INK)


def g_wing(d):   # a glider
    arc(d, 512, 470, 280, 195, 345, ry=170)
    arc(d, 512, 470, 280, 200, 340, ry=100, w=int(W * 0.7))
    for x in (250, 512, 774):
        line(d, [(x, 430 if x != 512 else 330), (512, 740)], w=int(W * 0.6))
    box(d, 476, 730, 548, 790, 16)


def g_wave(d):
    for y, f in ((420, INK), (600, DIM)):
        line(d, [(x, y + 56 * math.sin((x - 250) / 84)) for x in range(250, 780, 8)], fill=f)


def g_balloon(d):
    d.ellipse((512 - 170 - W / 2, 220 - W / 2, 512 + 170 + W / 2, 610 + W / 2), outline=INK, width=W)
    line(d, [(484, 650), (540, 650), (512, 614)], w=int(W * 0.8), closed=True)
    line(d, bezier((512, 660), (460, 720), (570, 760), (512, 820)), w=int(W * 0.6))


def _banana_pts(cx, cy, scale, turn):
    """A banana's outline: the lower arc of one circle and the flatter arc of a bigger one above it, meeting at the
    tips (15 and 165 degrees on the first), tilted by `turn` degrees and centred on (cx, cy). Returns the outline and
    the stem."""
    outer = [(math.cos(math.radians(a)), math.sin(math.radians(a))) for a in [15 + 150 * i / 40 for i in range(41)]]
    inner = [(1.472 * math.cos(math.radians(a)), -0.852 + 1.472 * math.sin(math.radians(a)))
             for a in [131 - 82 * i / 30 for i in range(1, 30)]]
    tip = outer[0]
    stem = [tip, (tip[0] + 0.07, tip[1] - 0.26)]
    t = math.radians(turn)

    def turned(pts):
        return [(300 * scale * (x * math.cos(t) - y * math.sin(t)), 300 * scale * (x * math.sin(t) + y * math.cos(t)))
                for x, y in pts]
    body, stem = turned(outer + inner), turned(stem)
    xs, ys = [x for x, _ in body + stem], [y for _, y in body + stem]
    dx, dy = cx - (min(xs) + max(xs)) / 2, cy - (min(ys) + max(ys)) / 2     # centred on (cx, cy)
    return [(x + dx, y + dy) for x, y in body], [(x + dx, y + dy) for x, y in stem]


def _banana(d, cx, cy, scale=1.0, turn=-18, fill=INK):
    body, stem = _banana_pts(cx, cy, scale, turn)
    d.polygon(body, fill=CLEAR)                       # hide whatever is behind it
    line(d, body, w=W * 2, fill=CLEAR, closed=True)
    line(d, stem, w=W * 2, fill=CLEAR)
    w = int(W * min(1.0, 0.25 + scale))
    line(d, body, w=w, fill=fill, closed=True)
    line(d, stem, w=w, fill=fill)


def g_banana(d):
    _banana(d, 512, 512, 1.15)


def g_bananas(d):   # a bunch: one banana in front of another
    _banana(d, 560, 430, 0.95, -30, DIM)
    _banana(d, 490, 580, 1.0, -12)


def g_bone(d):   # a fossil
    m, md = mask()
    md.rounded_rectangle((300, 462, 724, 562), radius=40, fill=255)
    for x in (300, 724):
        for y in (436, 588):
            md.ellipse((x - 82, y - 82, x + 82, y + 82), fill=255)
    m = m.rotate(-30, resample=Image.BICUBIC, center=(512, 512))
    outline_of(d, m)


def g_chest(d):
    box(d, 252, 300, 772, 560, 110)
    erase_box(d, 252, 480, 772, 760, 30)
    box(d, 252, 480, 772, 760, 30)
    erase_box(d, 464, 430, 560, 580, 22)
    box(d, 464, 430, 560, 580, 22, w=int(W * 0.85))


def g_fist(d):   # a skill learned: level up
    ring(d, 512, 512, 262)
    line(d, [(512, 660), (512, 372)])
    line(d, [(400, 484), (512, 372), (624, 484)])


def g_layers(d):   # the layers of the world
    line(d, [(512, 270), (780, 410), (512, 550), (244, 410)], closed=True)
    line(d, [(244, 520), (512, 660), (780, 520)])
    line(d, [(244, 630), (512, 770), (780, 630)], fill=DIM)


def g_core(d):   # the planet's core
    ring(d, 512, 512, 262, fill=DIM)
    ring(d, 512, 512, 160)
    dot(d, 512, 512, 66)


def g_palm(d):   # DK Island
    arc(d, 512, 800, 290, 200, 340, ry=120)
    line(d, bezier((520, 720), (500, 600), (520, 470), (560, 360)))
    top = (560, 350)
    for a, length in ((-175, 250), (-130, 220), (-55, 220), (-8, 250)):
        t = math.radians(a)
        pts = [(top[0] + length * k * math.cos(t), top[1] + length * k * math.sin(t) + 150 * k * k)
               for k in [i / 12 for i in range(13)]]
        line(d, pts, w=int(W * 0.9))


def g_emerald(d):
    pts = [(372, 280), (652, 280), (782, 430), (512, 782), (242, 430)]
    line(d, pts, closed=True)
    fw = int(W * 0.6)
    line(d, [(242, 430), (782, 430)], w=fw)
    line(d, [(372, 280), (432, 430), (512, 280), (592, 430), (652, 280)], w=fw)
    line(d, [(432, 430), (512, 782), (592, 430)], w=fw)


def tile(tier: str) -> Image.Image:
    img = Image.new("RGBA", (S, S))
    px = img.load()
    for y in range(S):
        t = y / (S - 1)
        c = tuple(round(TOP[i] + (BOTTOM[i] - TOP[i]) * t) for i in range(3)) + (255,)
        for x in range(S):
            px[x, y] = c
    glow = Image.new("RGBA", (S, S), CLEAR)
    ImageDraw.Draw(glow).ellipse((150, 120, 874, 844), fill=GLOW[tier])
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(150)))
    return img


def draw(glyph: str, tier: str) -> Image.Image:
    img = tile(tier)
    layer = Image.new("RGBA", (S, S), CLEAR)
    globals()["g_" + glyph](ImageDraw.Draw(layer))
    img.alpha_composite(layer)
    return img.resize((OUT, OUT), Image.LANCZOS)


def main() -> None:
    for title in switch.TITLES:
        folder = ROOT / "achtrack" / "assets" / "switch" / title.key
        folder.mkdir(parents=True, exist_ok=True)
        for goal in title.goals:
            glyph, tier = GOALS[goal.name]
            draw(glyph, tier).save(folder / f"{goal.name}.png", optimize=True)
        print(title.name, len(title.goals), "icons in", folder.relative_to(ROOT))


if __name__ == "__main__":
    main()
