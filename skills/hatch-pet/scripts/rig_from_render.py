#!/usr/bin/env python3
"""Build a complete v2 atlas from ONE canon render, by rigging it.

Instead of generating 73 frames with an image model (slow, quota-bound, and
identity-drifty), this cuts the creature out of a single high-quality render
and animates it with per-frame transforms: squash and stretch, lean, bob,
lift, collapse, plus ember-glow grading. Every frame is the same pixels, so
identity is exact by construction and the art tier is whatever the render is.

  python rig_from_render.py <render.png> --output-dir <frames-root> \
      [--chroma 00ff00] [--anchor-height 168]

Then compose/validate/contact-sheet as usual. Whole-body motion cannot fake a
wing-raise or a true head turn: waving reads as a greeting bounce and the look
rows as directional lean (legitimate body language for a chibi, and the v2
deadzone keeps neutral on idle). For per-limb posing, generate those rows with
a reference-capable model and drop them in over these.
"""
import argparse
import math
import os
from collections import deque

from PIL import Image, ImageChops, ImageEnhance

CELL_W, CELL_H = 192, 208
BOTTOM_PAD = 6


# ---------------------------------------------------------------- cutout ----
def knock_out(im, tol=60):
    """Remove the chroma background by flooding from the borders."""
    im = im.convert("RGB")
    w, h = im.size
    px = im.load()
    corners = [px[1, 1], px[w - 2, 1], px[1, h - 2], px[w - 2, h - 2]]
    bg = tuple(sorted(c[i] for c in corners)[len(corners) // 2] for i in range(3))

    near = bytearray(w * h)
    for y in range(h):
        row = y * w
        for x in range(w):
            r, g, b = px[x, y]
            if (r - bg[0]) ** 2 + (g - bg[1]) ** 2 + (b - bg[2]) ** 2 < tol * tol:
                near[row + x] = 1

    is_bg = bytearray(w * h)
    q = deque()
    for x in range(w):
        for y in (0, h - 1):
            i = y * w + x
            if near[i] and not is_bg[i]:
                is_bg[i] = 1
                q.append(i)
    for y in range(h):
        for x in (0, w - 1):
            i = y * w + x
            if near[i] and not is_bg[i]:
                is_bg[i] = 1
                q.append(i)
    while q:
        i = q.popleft()
        x, y = i % w, i // w
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if 0 <= nx < w and 0 <= ny < h:
                n = ny * w + nx
                if near[n] and not is_bg[n]:
                    is_bg[n] = 1
                    q.append(n)

    out = im.convert("RGBA")
    o = out.load()
    for y in range(h):
        for x in range(w):
            if is_bg[y * w + x]:
                o[x, y] = (0, 0, 0, 0)
            else:
                r, g, b, a = o[x, y]
                if g > r + 20 and g > b + 20:      # green spill anywhere
                    m = (r + b) // 2
                    o[x, y] = (r, m, b, a)
    box = out.getbbox()
    return out.crop(box) if box else out


def ember(img, k):
    """Grade the ember glow: k<1 banks it, k>1 makes it blaze."""
    if abs(k - 1.0) < 0.02:
        return img
    rgb = img.convert("RGB")
    if k > 1:
        rgb = ImageEnhance.Brightness(rgb).enhance(1 + (k - 1) * 0.30)
        rgb = ImageEnhance.Color(rgb).enhance(1 + (k - 1) * 0.55)
        r, g, b = rgb.split()
        r = ImageChops.add(r, r.point(lambda v: int(v * (k - 1) * 0.35)))
        rgb = Image.merge("RGB", (r, g, b))
    else:
        rgb = ImageEnhance.Brightness(rgb).enhance(0.55 + 0.45 * k)
        rgb = ImageEnhance.Color(rgb).enhance(max(0.25, k))
    out = rgb.convert("RGBA")
    out.putalpha(img.getchannel("A"))
    return out


def pose(base, *, rot=0.0, sx=1.0, sy=1.0, dx=0, dy=0, glow=1.0, flip=False):
    """Render one frame: transform the cutout into a 192x208 cell."""
    img = base
    if flip:
        img = img.transpose(Image.FLIP_LEFT_RIGHT)
    if abs(sx - 1) > 0.001 or abs(sy - 1) > 0.001:
        img = img.resize((max(1, round(img.width * sx)), max(1, round(img.height * sy))),
                         Image.LANCZOS)
    if abs(rot) > 0.01:
        img = img.rotate(rot, resample=Image.BICUBIC, expand=True)
    img = ember(img, glow)

    cell = Image.new("RGBA", (CELL_W, CELL_H), (0, 0, 0, 0))
    x = (CELL_W - img.width) // 2 + dx
    y = CELL_H - BOTTOM_PAD - img.height + dy
    cell.alpha_composite(img, (x, y))
    return cell


# ------------------------------------------------------------- choreography --
def recipes():
    """(state -> list of per-frame kwargs). Frame counts follow the v2 contract."""
    r = {}

    # idle: breathe + settle, embers banked
    r["idle"] = [
        dict(sy=1.000, dy=0, glow=0.90),
        dict(sy=1.008, dy=-1, glow=0.94),
        dict(sy=1.014, dy=-2, glow=0.99),
        dict(sy=1.012, dy=-2, glow=1.00),
        dict(sy=1.006, dy=-1, glow=0.95),
        dict(sy=0.996, dy=0, sx=1.006, glow=0.90),
    ]

    # running-right: bounding waddle — lean into travel, alternating hop
    hop = [0, -5, -8, -5, 0, -4, -7, -3]
    lean = [-4, -7, -9, -7, -4, -6, -8, -5]
    r["running-right"] = [
        dict(rot=lean[i], dy=hop[i], dx=(i % 2) * 2 - 1,
             sy=1.0 - hop[i] * 0.004, glow=1.05)
        for i in range(8)
    ]
    r["running-left"] = [dict(d, flip=True, rot=-d["rot"], dx=-d["dx"])
                         for d in (dict(x) for x in r["running-right"])]

    # waving: greeting bounce — rock back, up, return, settle
    r["waving"] = [
        dict(rot=-6, dy=-4, sy=1.02, glow=1.12),
        dict(rot=-11, dy=-9, sy=1.04, glow=1.22),
        dict(rot=-5, dy=-4, sy=1.02, glow=1.14),
        dict(rot=0, dy=0, sy=1.00, glow=1.02),
    ]

    # jumping: anticipation, launch, peak, descent, land
    r["jumping"] = [
        dict(sy=0.90, sx=1.08, dy=2, glow=1.0),
        dict(sy=1.10, sx=0.94, dy=-14, glow=1.15),
        dict(sy=1.06, sx=0.97, dy=-26, rot=-3, glow=1.25),
        dict(sy=1.04, sx=0.98, dy=-12, rot=2, glow=1.10),
        dict(sy=0.93, sx=1.06, dy=1, glow=0.98),
    ]

    # failed: slump, sink, flatten into a mound; embers die back to one point
    r["failed"] = [
        dict(rot=3, sy=0.97, dy=2, glow=0.85),
        dict(rot=6, sy=0.92, sx=1.02, dy=5, glow=0.72),
        dict(rot=9, sy=0.85, sx=1.05, dy=9, glow=0.60),
        dict(rot=11, sy=0.76, sx=1.09, dy=13, glow=0.48),
        dict(rot=12, sy=0.66, sx=1.13, dy=17, glow=0.38),
        dict(rot=12, sy=0.58, sx=1.17, dy=20, glow=0.30),
        dict(rot=12, sy=0.55, sx=1.19, dy=21, glow=0.34),
        dict(rot=12, sy=0.55, sx=1.19, dy=21, glow=0.28),
    ]

    # waiting: head-up plea, embers pulsing
    r["waiting"] = [
        dict(rot=-7, dy=-2, glow=1.00),
        dict(rot=-10, dy=-4, glow=1.18),
        dict(rot=-11, dy=-5, glow=1.26),
        dict(rot=-10, dy=-4, glow=1.14),
        dict(rot=-8, dy=-2, glow=1.00),
        dict(rot=-6, dy=-1, glow=0.92),
    ]

    # running (work): hunch forward, fast bob, embers at full blaze
    r["running"] = [
        dict(rot=7, dy=0, sy=0.99, glow=1.30),
        dict(rot=9, dy=-3, sy=1.01, glow=1.40),
        dict(rot=8, dy=-1, sy=1.00, glow=1.34),
        dict(rot=10, dy=-4, sy=1.02, glow=1.44),
        dict(rot=8, dy=-1, sy=0.99, glow=1.32),
        dict(rot=7, dy=0, sy=0.98, glow=1.26),
    ]

    # review: bow over the work, small considering tilts
    r["review"] = [
        dict(rot=10, dy=3, sy=0.98, glow=1.02),
        dict(rot=13, dy=4, sy=0.97, glow=1.06),
        dict(rot=11, dy=3, sy=0.98, glow=1.02),
        dict(rot=14, dy=5, sy=0.96, glow=1.08),
        dict(rot=12, dy=4, sy=0.97, glow=1.04),
        dict(rot=10, dy=3, sy=0.98, glow=1.00),
    ]
    return r


def look_recipe(deg):
    """Directional attention: lean toward the target, lift for up, bow for down.
    000 = up, clockwise. Whole-body body language, not a fake head rotation."""
    th = math.radians(deg)
    lean = math.sin(th) * 11.0          # +right / -left
    pitch = math.cos(th)                # +up / -down
    return dict(rot=lean,
                dy=round(-pitch * 5),
                sy=1.0 + pitch * 0.015,
                sx=1.0 - abs(math.sin(th)) * 0.02,
                glow=1.0 + pitch * 0.05)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("render")
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--chroma-tol", type=int, default=60)
    ap.add_argument("--anchor-height", type=int, default=168)
    args = ap.parse_args()

    cut = knock_out(Image.open(args.render), args.chroma_tol)
    k = min(args.anchor_height / cut.height, (CELL_W - 20) / cut.width)
    cut = cut.resize((max(1, round(cut.width * k)), max(1, round(cut.height * k))),
                     Image.LANCZOS)
    print(f"cutout {cut.size} (scaled x{k:.3f})")

    made = 0
    for state, frames in recipes().items():
        d = os.path.join(args.output_dir, state)
        os.makedirs(d, exist_ok=True)
        for i, kw in enumerate(frames):
            pose(cut, **kw).save(os.path.join(d, f"{i:02d}.png"))
            made += 1

    look_dir = os.path.join(args.output_dir, "look")
    os.makedirs(look_dir, exist_ok=True)
    for i in range(16):
        deg = i * 22.5
        name = f"{int(deg):03d}" if deg == int(deg) else f"{deg:05.1f}"
        pose(cut, **look_recipe(deg)).save(os.path.join(look_dir, name + ".png"))
        made += 1

    print(f"rigged {made} frames -> {args.output_dir}")


if __name__ == "__main__":
    main()
