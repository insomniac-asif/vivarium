#!/usr/bin/env python3
"""Build a complete v2 atlas from ONE canon render, by rigging it.

Instead of generating 73 frames with an image model (slow, quota-bound, and
identity-drifty), this cuts the creature out of a single high-quality render
and animates it: squash and stretch, a non-rigid BEND so the body follows
through instead of pivoting like cardboard, arcing bobs, overshoot, and
ember-glow grading. Every frame is the same pixels, so identity is exact by
construction and the art tier is whatever the render is.

  python rig_from_render.py <render.png> --output-dir <frames-root> \
      [--chroma-tol 60] [--anchor-height 168]

Then compose/validate/contact-sheet as usual. Whole-body motion cannot pose a
limb: waving reads as a greeting bounce and the look rows as the body aiming
its attention. For per-limb posing, generate those rows with a
reference-capable model and drop them in over these.
"""
import argparse
import math
import os
from collections import deque

from PIL import Image, ImageChops, ImageEnhance, ImageFilter

CELL_W, CELL_H = 192, 208
BOTTOM_PAD = 6


# ---------------------------------------------------------------- cutout ----
def knock_out(im, tol=60):
    """Remove the backdrop by flooding inward from the borders.

    The tolerance ADAPTS to the backdrop: studio renders use a smooth gradient,
    so a single sampled colour plus a fixed tolerance leaves a visible
    rectangle behind. Sampling the whole border and widening the tolerance to
    cover its spread follows the gradient, while a contrasting subject stays
    far outside it. (Region-growing on local difference was tried and leaks
    straight through a dark subject's own shading.)
    """
    im = im.convert("RGB")
    w, h = im.size
    px = im.load()

    ring = ([px[x, 1] for x in range(1, w - 1, max(1, w // 24))] +
            [px[x, h - 2] for x in range(1, w - 1, max(1, w // 24))] +
            [px[1, y] for y in range(1, h - 1, max(1, h // 24))] +
            [px[w - 2, y] for y in range(1, h - 1, max(1, h // 24))])
    bg = tuple(sorted(c[i] for c in ring)[len(ring) // 2] for i in range(3))
    # robust spread: the 80th percentile, so subject pixels clipped by the crop
    # edge cannot inflate the tolerance into erasing the whole image
    dists = sorted((sum((c[i] - bg[i]) ** 2 for i in range(3))) ** 0.5 for c in ring)
    spread = dists[int(len(dists) * 0.8)] if dists else 0
    eff = max(tol, min(spread * 1.35 + 18, 110))

    def is_near(c):
        return ((c[0] - bg[0]) ** 2 + (c[1] - bg[1]) ** 2 + (c[2] - bg[2]) ** 2) < eff * eff

    near = bytearray(w * h)
    for y in range(h):
        row = y * w
        for x in range(w):
            if is_near(px[x, y]):
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
    out = drop_trapped_backdrop(out)
    out = largest_piece(out)
    box = out.getbbox()
    return out.crop(box) if box else out


def drop_trapped_backdrop(img, min_blob=280):
    """Clear backdrop caught INSIDE the silhouette — e.g. the gap between a
    tail and a leg, which a border flood can never reach.

    Only large contiguous blobs of bright, colourless pixels are removed, so
    scattered specular highlights on the subject survive.
    """
    w, h = img.size
    px = img.load()

    def backdroppy(c):
        if c[3] <= 8:
            return False
        lum = (c[0] * 2 + c[1] * 3 + c[2]) // 6
        return lum > 145 and (max(c[:3]) - min(c[:3])) < 30   # bright + neutral

    seen = bytearray(w * h)
    cleared = 0
    for start in range(w * h):
        sx, sy = start % w, start // w
        if seen[start] or not backdroppy(px[sx, sy]):
            continue
        q = deque([start]); seen[start] = 1; blob = [start]
        while q:
            i = q.popleft(); x, y = i % w, i // w
            for nx, ny in ((x-1, y), (x+1, y), (x, y-1), (x, y+1)):
                if 0 <= nx < w and 0 <= ny < h:
                    n = ny * w + nx
                    if not seen[n] and backdroppy(px[nx, ny]):
                        seen[n] = 1; q.append(n); blob.append(n)
        if len(blob) >= min_blob:
            for i in blob:
                px[i % w, i // w] = (0, 0, 0, 0)
            cleared += len(blob)
    return img


def largest_piece(img):
    """Drop everything except the biggest connected blob — neighbouring
    figures and stray fragments caught by the crop."""
    w, h = img.size
    a = img.getchannel("A").load()
    seen = bytearray(w * h)
    best, best_area = None, 0
    for start in range(w * h):
        if seen[start] or a[start % w, start // w] <= 8:
            continue
        q = deque([start]); seen[start] = 1; cells = [start]
        while q:
            i = q.popleft(); x, y = i % w, i // w
            for nx, ny in ((x-1, y), (x+1, y), (x, y-1), (x, y+1)):
                if 0 <= nx < w and 0 <= ny < h:
                    n = ny * w + nx
                    if not seen[n] and a[nx, ny] > 8:
                        seen[n] = 1; q.append(n); cells.append(n)
        if len(cells) > best_area:
            best_area, best = len(cells), cells
    if not best or best_area == w * h:
        return img
    keep = bytearray(w * h)
    for i in best:
        keep[i] = 1
    px = img.load()
    for y in range(h):
        row = y * w
        for x in range(w):
            if not keep[row + x]:
                px[x, y] = (0, 0, 0, 0)
    return img


# ------------------------------------------------------------ deformation ---
def bend(img, amp, power=2.1):
    """Non-rigid lean: displace each scanline horizontally, growing toward the
    top. This is what keeps the creature from reading as a rotating cutout —
    the base stays planted while the head and crest carry the motion."""
    if abs(amp) < 0.4:
        return img
    w, h = img.size
    pad = int(abs(amp)) + 2
    out = Image.new("RGBA", (w + 2 * pad, h), (0, 0, 0, 0))
    denom = max(1, h - 1)
    for y in range(h):
        t = (denom - y) / denom            # 0 at the base, 1 at the top
        dx = amp * (t ** power)
        out.alpha_composite(img.crop((0, y, w, y + 1)), (pad + int(round(dx)), y))
    return out


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
        rgb = ImageEnhance.Brightness(rgb).enhance(0.86 + 0.14 * k)
        rgb = ImageEnhance.Color(rgb).enhance(max(0.6, k))
    out = rgb.convert("RGBA")
    out.putalpha(img.getchannel("A"))
    return out


def pose(base, *, rot=0.0, lean=0.0, sx=1.0, sy=1.0, dx=0, dy=0, glow=1.0,
         flip=False):
    """One frame: squash, bend, tilt about the FEET, grade, place in the cell."""
    img = base
    if flip:
        img = img.transpose(Image.FLIP_LEFT_RIGHT)
    if abs(sx - 1) > 0.001 or abs(sy - 1) > 0.001:
        img = img.resize((max(1, round(img.width * sx)), max(1, round(img.height * sy))),
                         Image.LANCZOS)
    if abs(lean) > 0.4:
        img = bend(img, lean)
    if abs(rot) > 0.01:
        # pivot at the base so a tilt reads as weight shifting, not spinning
        img = img.rotate(rot, resample=Image.BICUBIC, expand=True,
                         center=(img.width / 2, img.height - 1))
    img = ember(img, glow)

    cell = Image.new("RGBA", (CELL_W, CELL_H), (0, 0, 0, 0))
    cell.alpha_composite(img, ((CELL_W - img.width) // 2 + dx,
                               CELL_H - BOTTOM_PAD - img.height + dy))
    return cell


# ------------------------------------------------------------- choreography --
def recipes():
    """state -> per-frame kwargs. Frame counts follow the v2 contract.
    Amplitudes are deliberately generous: at 192px, timid motion reads as a
    still image. Bend carries most of the life; rotation is a seasoning."""
    r = {}

    # idle — a slow breath with a drifting sway, embers banked low
    r["idle"] = [
        dict(sy=1.000, sx=1.000, dy=0, lean=1.5, glow=0.86),
        dict(sy=1.014, sx=0.994, dy=-2, lean=4.0, glow=0.92),
        dict(sy=1.026, sx=0.988, dy=-4, lean=5.5, glow=0.99),
        dict(sy=1.022, sx=0.990, dy=-4, lean=2.0, glow=1.02),
        dict(sy=1.010, sx=0.996, dy=-2, lean=-2.5, glow=0.94),
        dict(sy=0.992, sx=1.010, dy=1, lean=-4.5, glow=0.86),
    ]

    # running-right — bounding hop; the body trails the leap, then whips forward
    hop = [0, -7, -12, -8, -1, -6, -11, -4]
    trail = [-5, -10, -4, 6, 9, -3, -9, -2]
    r["running-right"] = [
        dict(lean=trail[i], rot=-3 - hop[i] * 0.15, dy=hop[i],
             dx=(1 if i % 2 else -1) * 2,
             sy=1.0 - hop[i] * 0.006, sx=1.0 + hop[i] * 0.004, glow=1.06)
        for i in range(8)
    ]
    r["running-left"] = [dict(f, flip=True, rot=-f["rot"], lean=-f["lean"],
                              dx=-f["dx"]) for f in map(dict, r["running-right"])]

    # waving — rock back hard, overshoot forward, settle
    r["waving"] = [
        dict(lean=-9, rot=-4, dy=-4, sy=1.03, sx=0.98, glow=1.12),
        dict(lean=-19, rot=-8, dy=-12, sy=1.07, sx=0.95, glow=1.26),
        dict(lean=7, rot=3, dy=-4, sy=1.00, sx=1.02, glow=1.14),
        dict(lean=-2, rot=0, dy=0, sy=1.00, sx=1.00, glow=1.02),
    ]

    # jumping — deep anticipation, stretched launch, floating peak, land squash
    r["jumping"] = [
        dict(sy=0.84, sx=1.15, dy=5, lean=2, glow=0.98),
        dict(sy=1.17, sx=0.89, dy=-20, lean=-5, glow=1.18),
        dict(sy=1.07, sx=0.96, dy=-36, lean=4, rot=-4, glow=1.30),
        dict(sy=1.01, sx=1.00, dy=-15, lean=9, rot=5, glow=1.10),
        dict(sy=0.87, sx=1.12, dy=3, lean=-4, glow=0.96),
    ]

    # failed — slump sideways, buckle, flatten into a mound, embers dying
    r["failed"] = [
        dict(lean=4, rot=2, sy=0.96, sx=1.02, dy=3, glow=0.82),
        dict(lean=9, rot=5, sy=0.90, sx=1.05, dy=7, glow=0.68),
        dict(lean=14, rot=8, sy=0.82, sx=1.09, dy=12, glow=0.56),
        dict(lean=18, rot=10, sy=0.72, sx=1.14, dy=17, glow=0.45),
        dict(lean=20, rot=11, sy=0.62, sx=1.19, dy=21, glow=0.36),
        dict(lean=21, rot=11, sy=0.54, sx=1.23, dy=24, glow=0.29),
        dict(lean=21, rot=11, sy=0.52, sx=1.25, dy=25, glow=0.33),
        dict(lean=21, rot=11, sy=0.52, sx=1.25, dy=25, glow=0.26),
    ]

    # waiting — cranes up at you, embers pulsing like held breath
    r["waiting"] = [
        dict(lean=-6, rot=-2, dy=-3, sy=1.02, glow=1.00),
        dict(lean=-12, rot=-4, dy=-7, sy=1.05, glow=1.20),
        dict(lean=-14, rot=-5, dy=-9, sy=1.06, glow=1.30),
        dict(lean=-12, rot=-4, dy=-7, sy=1.05, glow=1.16),
        dict(lean=-9, rot=-3, dy=-4, sy=1.03, glow=1.00),
        dict(lean=-6, rot=-2, dy=-2, sy=1.01, glow=0.90),
    ]

    # running (work) — hunched into the task, fast bob, embers at full blaze
    r["running"] = [
        dict(lean=9, rot=3, dy=0, sy=0.98, sx=1.02, glow=1.30),
        dict(lean=13, rot=4, dy=-5, sy=1.02, sx=0.99, glow=1.42),
        dict(lean=10, rot=3, dy=-1, sy=0.99, sx=1.01, glow=1.34),
        dict(lean=15, rot=5, dy=-6, sy=1.03, sx=0.98, glow=1.46),
        dict(lean=11, rot=3, dy=-2, sy=0.99, sx=1.01, glow=1.32),
        dict(lean=8, rot=2, dy=0, sy=0.97, sx=1.02, glow=1.24),
    ]

    # review — bows over the finished work, small considering shifts
    r["review"] = [
        dict(lean=12, rot=4, dy=4, sy=0.97, sx=1.02, glow=1.00),
        dict(lean=17, rot=6, dy=6, sy=0.95, sx=1.04, glow=1.06),
        dict(lean=13, rot=4, dy=4, sy=0.97, sx=1.02, glow=1.00),
        dict(lean=19, rot=6, dy=7, sy=0.94, sx=1.05, glow=1.08),
        dict(lean=15, rot=5, dy=5, sy=0.96, sx=1.03, glow=1.02),
        dict(lean=11, rot=3, dy=3, sy=0.98, sx=1.01, glow=0.98),
    ]
    return r


def look_recipe(deg):
    """Attention aimed by the BODY: the base stays planted, the head and crest
    lean toward the target. 000 = up, clockwise."""
    th = math.radians(deg)
    horiz = math.sin(th)
    vert = math.cos(th)
    return dict(lean=horiz * 15.0,
                rot=horiz * 3.0,
                dy=round(-vert * 6),
                sy=1.0 + vert * 0.030,
                sx=1.0 - abs(horiz) * 0.025,
                glow=1.0 + vert * 0.06)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("render")
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--chroma-tol", type=int, default=60)
    ap.add_argument("--anchor-height", type=int, default=168)
    args = ap.parse_args()

    cut = knock_out(Image.open(args.render), args.chroma_tol)
    k = min(args.anchor_height / cut.height, (CELL_W - 26) / cut.width)
    cut = cut.resize((max(1, round(cut.width * k)), max(1, round(cut.height * k))),
                     Image.LANCZOS)
    # detail survives the downscale only if it is restored afterwards
    cut = cut.filter(ImageFilter.UnsharpMask(radius=1.1, percent=125, threshold=2))
    print(f"cutout {cut.size} (scaled x{k:.3f}, sharpened)")

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
