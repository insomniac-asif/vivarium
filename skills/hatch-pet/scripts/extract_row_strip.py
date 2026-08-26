#!/usr/bin/env python3
"""Extract pose cells from an AI-generated horizontal row strip.

Takes a strip of character poses drawn on a solid chroma-key background and
produces uniform 192x208 RGBA cells named 00.png, 01.png, ...:

1. Background removal -- pixels within a tolerance of the chroma key (RGB
   Euclidean distance) become fully transparent; a feather band just outside
   the tolerance maps to partial alpha so anti-aliased edges stay soft.
2. Pose grouping -- ``--method components`` (default) scans column occupancy
   and groups connected non-transparent spans left to right, merging
   fragments whose horizontal gap is under 8 px (a detached hand or ear stays
   with its body); ``--method grid`` slices the strip into N equal slots.
3. Normalization -- every pose is scaled by ONE shared factor, chosen so the
   largest pose fits 192x208 with 6 px padding (the widest pose also bounds
   the factor so nothing overflows horizontally), then anchored at the
   bottom-center baseline of its cell.
4. Despill -- edge pixels whose hue lies within 30 degrees of the chroma key
   hue are blended toward the neighboring interior color.

A JSON report ``{ok, frames_found, errors, warnings, cells}`` is written to
``--json-out`` (when given) and a one-line summary is printed. The script
never raises on bad input; failures are reported in the JSON and via a
non-zero exit code. Stdlib + Pillow only.
"""

from __future__ import annotations

import argparse
import colorsys
import json
import math
import os
import sys

from PIL import Image

CELL_W = 192
CELL_H = 208
PADDING = 6
MERGE_GAP_PX = 8         # component fragments closer than this are one pose
OCCUPIED_ALPHA = 8       # min alpha for a pixel to count as foreground
LOW_ALPHA = 32           # neighbor alpha below this marks a pixel as "edge"
INTERIOR_ALPHA = 200     # neighbor alpha at/above this counts as interior
MIN_SATURATION = 0.12    # below this, hue is noise and despill is skipped
DESPILL_HUE_DEG = 30.0   # hue window around the key hue
DESPILL_BLEND = 0.6      # how far spill pixels move toward interior color

RESAMPLE = getattr(Image, "Resampling", Image).LANCZOS


def pixel_tuples(img):
    """Per-pixel tuples; getdata() is deprecated in Pillow 12+."""
    getter = getattr(img, "get_flattened_data", img.getdata)
    return getter()


def parse_hex_color(text):
    """Parse '00ff00', '#ff00ff', etc. into an (r, g, b) tuple."""
    s = text.strip().lstrip("#")
    if len(s) != 6:
        raise ValueError(f"chroma key must be 6 hex digits, got {text!r}")
    try:
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        raise ValueError(f"chroma key is not valid hex: {text!r}") from None


def hue_of(r, g, b):
    """Hue in [0, 1) plus saturation, from 0-255 RGB."""
    h, s, _v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
    return h, s


def hue_distance_deg(h1, h2):
    """Circular distance between two [0, 1) hues, in degrees."""
    d = abs(h1 - h2)
    return min(d, 1.0 - d) * 360.0


def key_out_background(img, key, tolerance, feather):
    """Return a copy of ``img`` with near-key pixels made transparent.

    Alpha ramps from 0 to the original value as RGB distance to the key goes
    from ``tolerance`` to ``tolerance + feather``.
    """
    kr, kg, kb = key
    lo = max(tolerance, 0.0)
    hi = lo + max(feather, 1e-6)
    lo2, hi2 = lo * lo, hi * hi
    span = hi - lo
    out = []
    for r, g, b, a in pixel_tuples(img):
        d2 = (r - kr) ** 2 + (g - kg) ** 2 + (b - kb) ** 2
        if d2 <= lo2:
            out.append((r, g, b, 0))
        elif d2 >= hi2:
            out.append((r, g, b, a))
        else:
            t = (math.sqrt(d2) - lo) / span
            out.append((r, g, b, int(a * t)))
    result = Image.new("RGBA", img.size)
    result.putdata(out)
    return result


def runs_from_projection(projection):
    """Turn a 0/1 column-occupancy list into [start, end) column runs."""
    runs = []
    start = None
    for x, occupied in enumerate(projection):
        if occupied and start is None:
            start = x
        elif not occupied and start is not None:
            runs.append((start, x))
            start = None
    if start is not None:
        runs.append((start, len(projection)))
    return runs


def merge_close_runs(runs, max_gap):
    """Merge adjacent runs separated by less than ``max_gap`` columns."""
    merged = [runs[0]]
    for x0, x1 in runs[1:]:
        if x0 - merged[-1][1] < max_gap:
            merged[-1] = (merged[-1][0], x1)
        else:
            merged.append((x0, x1))
    return merged


def groups_from_components(mask, expected, warnings):
    """Group foreground columns into pose bounding boxes, left to right.

    ``mask`` is an "L" image, 255 where foreground. Connectivity is by column
    projection: spans separated only by fully transparent column gaps under
    MERGE_GAP_PX are one pose. If more groups remain than expected, the pair
    with the smallest gap is merged repeatedly (fragmented art); if fewer are
    found, that is reported and the found groups are emitted.
    """
    w, h = mask.size
    x_projection, _y = mask.getprojection()
    runs = merge_close_runs(runs_from_projection(x_projection), MERGE_GAP_PX)

    if len(runs) > expected:
        warnings.append(
            f"found {len(runs)} components for {expected} expected frames; "
            "merged closest fragments"
        )
        while len(runs) > expected:
            gaps = [(runs[i + 1][0] - runs[i][1], i) for i in range(len(runs) - 1)]
            _gap, i = min(gaps)
            runs[i] = (runs[i][0], runs[i + 1][1])
            del runs[i + 1]
    elif len(runs) < expected:
        warnings.append(
            f"found only {len(runs)} pose groups, expected {expected}; "
            "poses may overlap -- consider --method grid"
        )

    boxes = []
    for x0, x1 in runs:
        bbox = mask.crop((x0, 0, x1, h)).getbbox()  # (l, t, r, b) in the crop
        boxes.append((x0 + bbox[0], bbox[1], x0 + bbox[2], bbox[3]))
    return boxes


def groups_from_grid(mask, expected, warnings):
    """Slice the strip into ``expected`` equal slots; bbox per slot or None."""
    w, h = mask.size
    boxes = []
    for i in range(expected):
        x0 = round(i * w / expected)
        x1 = round((i + 1) * w / expected)
        bbox = mask.crop((x0, 0, x1, h)).getbbox()
        if bbox is None:
            warnings.append(f"grid slot {i} is empty; emitting a blank cell")
            boxes.append(None)
        else:
            boxes.append((x0 + bbox[0], bbox[1], x0 + bbox[2], bbox[3]))
    return boxes


def bleed_transparent_rgb(img, passes=3):
    """Push interior colors outward into fully transparent border pixels.

    Transparent pixels keep the chroma color in RGB after keying; resampling
    would smear it back into the silhouette. Each pass fills unknown pixels
    that touch known ones with the average of those neighbors (alpha stays 0).
    """
    w, h = img.size
    px = img.load()
    known = [[px[x, y][3] > 0 for x in range(w)] for y in range(h)]
    for _ in range(passes):
        fills = []
        for y in range(h):
            for x in range(w):
                if known[y][x]:
                    continue
                rs = gs = bs = n = 0
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        if dx == 0 and dy == 0:
                            continue
                        nx, ny = x + dx, y + dy
                        if 0 <= nx < w and 0 <= ny < h and known[ny][nx]:
                            nr, ng, nb, _na = px[nx, ny]
                            rs += nr
                            gs += ng
                            bs += nb
                            n += 1
                if n:
                    fills.append((x, y, rs // n, gs // n, bs // n))
        if not fills:
            break
        for x, y, r, g, b in fills:
            px[x, y] = (r, g, b, 0)
            known[y][x] = True


def despill_edges(img, key):
    """Remove chroma spill from edge pixels, in place.

    Edge pixels (partial alpha, or bordering transparency) whose hue is within
    DESPILL_HUE_DEG of the key hue are blended toward the average neighboring
    interior color; with no clean interior neighbor they are desaturated
    toward their own gray. Returns the number of pixels adjusted.
    """
    key_hue, _ = hue_of(*key)
    w, h = img.size
    src = img.copy().load()  # read from a snapshot, write to the original
    px = img.load()
    fixed = 0
    for y in range(h):
        for x in range(w):
            r, g, b, a = src[x, y]
            if a == 0:
                continue
            edge = a < 255
            if not edge:
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        nx, ny = x + dx, y + dy
                        if not (0 <= nx < w and 0 <= ny < h):
                            edge = True  # image border counts as an edge
                        elif src[nx, ny][3] < LOW_ALPHA:
                            edge = True
                if not edge:
                    continue
            hue, sat = hue_of(r, g, b)
            if sat < MIN_SATURATION or hue_distance_deg(hue, key_hue) > DESPILL_HUE_DEG:
                continue
            rs = gs = bs = n = 0
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    nx, ny = x + dx, y + dy
                    if not (0 <= nx < w and 0 <= ny < h):
                        continue
                    nr, ng, nb, na = src[nx, ny]
                    if na < INTERIOR_ALPHA:
                        continue
                    nh, ns = hue_of(nr, ng, nb)
                    if ns >= MIN_SATURATION and hue_distance_deg(nh, key_hue) <= DESPILL_HUE_DEG:
                        continue  # neighbor is spill-tinted too
                    rs += nr
                    gs += ng
                    bs += nb
                    n += 1
            if n:
                r = round(r * (1 - DESPILL_BLEND) + (rs / n) * DESPILL_BLEND)
                g = round(g * (1 - DESPILL_BLEND) + (gs / n) * DESPILL_BLEND)
                b = round(b * (1 - DESPILL_BLEND) + (bs / n) * DESPILL_BLEND)
            else:
                gray = round(0.299 * r + 0.587 * g + 0.114 * b)
                r = round((r + gray) / 2)
                g = round((g + gray) / 2)
                b = round((b + gray) / 2)
            px[x, y] = (r, g, b, a)
            fixed += 1
    return fixed


def run(args):
    """Do the extraction; returns the report dict. May raise on I/O errors."""
    report = {"ok": False, "frames_found": 0, "errors": [], "warnings": [], "cells": []}

    if args.expected_frames < 1:
        report["errors"].append(f"--expected-frames must be >= 1, got {args.expected_frames}")
        return report
    try:
        key = parse_hex_color(args.chroma_key)
    except ValueError as exc:
        report["errors"].append(str(exc))
        return report
    if not os.path.isfile(args.strip):
        report["errors"].append(f"strip image not found: {args.strip}")
        return report
    try:
        with Image.open(args.strip) as src:
            img = src.convert("RGBA")
    except Exception as exc:
        report["errors"].append(f"cannot open strip image: {exc}")
        return report

    keyed = key_out_background(img, key, args.tolerance, args.feather)
    mask = keyed.getchannel("A").point(lambda a: 255 if a > OCCUPIED_ALPHA else 0)
    if mask.getbbox() is None:
        report["errors"].append(
            "no foreground content left after chroma removal; wrong --chroma-key "
            "or --tolerance too high?"
        )
        return report

    warnings = report["warnings"]
    if args.method == "grid":
        boxes = groups_from_grid(mask, args.expected_frames, warnings)
    else:
        boxes = groups_from_components(mask, args.expected_frames, warnings)

    present = [b for b in boxes if b is not None]
    max_w = max(b[2] - b[0] for b in present)
    max_h = max(b[3] - b[1] for b in present)
    avail_w = CELL_W - 2 * PADDING
    avail_h = CELL_H - 2 * PADDING
    scale = min(avail_w / max_w, avail_h / max_h)

    os.makedirs(args.output_dir, exist_ok=True)
    for i, box in enumerate(boxes):
        cell = Image.new("RGBA", (CELL_W, CELL_H), (0, 0, 0, 0))
        if box is not None:
            pose = keyed.crop(box)
            bleed_transparent_rgb(pose)
            sw = max(1, round(pose.width * scale))
            sh = max(1, round(pose.height * scale))
            scaled = pose.resize((sw, sh), RESAMPLE)
            despill_edges(scaled, key)
            cell.paste(scaled, ((CELL_W - sw) // 2, CELL_H - PADDING - sh), scaled)
        name = f"{i:02d}.png"
        cell.save(os.path.join(args.output_dir, name))
        report["cells"].append(name)

    report["frames_found"] = len(present)
    report["ok"] = True
    return report


def build_parser():
    p = argparse.ArgumentParser(
        description="Extract 192x208 pose cells from a chroma-keyed row strip."
    )
    p.add_argument("strip", help="path to the horizontal row-strip image")
    p.add_argument("--expected-frames", type=int, required=True,
                   help="number of poses the strip should contain")
    p.add_argument("--chroma-key", required=True,
                   help="background color as hex, e.g. 00ff00 or ff00ff")
    p.add_argument("--output-dir", required=True,
                   help="directory for the emitted 00.png..NN.png cells")
    p.add_argument("--json-out", default=None,
                   help="path to write the JSON report")
    p.add_argument("--method", choices=("grid", "components"), default="components",
                   help="pose grouping: connected components or N equal slots")
    p.add_argument("--tolerance", type=float, default=60.0,
                   help="RGB distance below which a pixel is background (default 60)")
    p.add_argument("--feather", type=float, default=40.0,
                   help="RGB distance band over which alpha ramps up (default 40)")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        report = run(args)
    except Exception as exc:  # contract: never crash -- report in the JSON
        report = {
            "ok": False,
            "frames_found": 0,
            "errors": [f"{type(exc).__name__}: {exc}"],
            "warnings": [],
            "cells": [],
        }
    if args.json_out:
        try:
            parent = os.path.dirname(os.path.abspath(args.json_out))
            os.makedirs(parent, exist_ok=True)
            with open(args.json_out, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2)
        except OSError as exc:
            print(f"warning: could not write --json-out: {exc}", file=sys.stderr)
    status = "ok" if report["ok"] else "FAILED"
    print(
        f"extract_row_strip: {status} frames_found={report['frames_found']} "
        f"cells={len(report['cells'])} errors={len(report['errors'])} "
        f"warnings={len(report['warnings'])} -> {args.output_dir}"
    )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
