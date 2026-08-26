"""Build the frames/ tree for compose_atlas.py from a slot->crop assignment.

assignment.json: { "idle/00": {"crop": "idle-03"}, "running-left/00":
{"crop": "running-right-02", "mirror": true}, "look/090": {...}, ... }

Each crop: background removed (border-connected flood of near-bg color),
green-spill desaturated at edges, scaled by ONE global factor anchored to the
assignment's designated anchor crop, bottom-center aligned into 192x208.
"""
import json
import os
import sys
from collections import deque
from PIL import Image

RUN = os.path.expanduser(r"~/.claude/pets/.hatch/huma")
LIB = os.path.join(RUN, "library")
OUT = os.path.join(RUN, "frames-final")
CELL_W, CELL_H = 192, 208
TOL = 55
ANCHOR_HEIGHT = 168.0  # standing body height in the cell
BOTTOM_PAD = 8


def load_crop(cid):
    sheet, n = cid.rsplit("-", 1)
    return Image.open(os.path.join(LIB, sheet, f"{n}.png")).convert("RGB")


def bg_color(im):
    w, h = im.size
    pts = [im.getpixel(p) for p in [(1, 1), (w - 2, 1), (1, h - 2), (w - 2, h - 2)]]
    return tuple(sorted(c[i] for c in pts)[len(pts) // 2] for i in range(3))


def knock_out(im):
    """Remove background: flood from borders through near-bg pixels."""
    w, h = im.size
    bg = bg_color(im)
    px = im.load()
    is_bg = bytearray(w * h)
    near = bytearray(w * h)
    for y in range(h):
        row = y * w
        for x in range(w):
            r, g, b = px[x, y]
            d2 = (r - bg[0]) ** 2 + (g - bg[1]) ** 2 + (b - bg[2]) ** 2
            if d2 < TOL * TOL:
                near[row + x] = 1
    q = deque()
    for x in range(w):
        for y in (0, h - 1):
            i = y * w + x
            if near[i] and not is_bg[i]:
                is_bg[i] = 1; q.append(i)
    for y in range(h):
        for x in (0, w - 1):
            i = y * w + x
            if near[i] and not is_bg[i]:
                is_bg[i] = 1; q.append(i)
    while q:
        i = q.popleft()
        x, y = i % w, i // w
        for nx, ny in ((x-1, y), (x+1, y), (x, y-1), (x, y+1)):
            if 0 <= nx < w and 0 <= ny < h:
                n = ny * w + nx
                if near[n] and not is_bg[n]:
                    is_bg[n] = 1; q.append(n)
    rgba = im.convert("RGBA")
    apx = rgba.load()
    for y in range(h):
        for x in range(w):
            if is_bg[y * w + x]:
                apx[x, y] = (0, 0, 0, 0)
            else:
                r, g, b, a = apx[x, y]
                # despill anywhere: the bird has no legitimate green
                if g > r + 20 and g > b + 20:
                    m = (r + b) // 2
                    apx[x, y] = (r, m, b, a)
    bbox = rgba.getbbox()
    return rgba.crop(bbox) if bbox else rgba


def main(assign_path):
    assign = json.load(open(assign_path, encoding="utf-8"))
    assign.pop("_anchor", None)
    cut = {}
    for slot, spec in assign.items():
        cid = spec["crop"]
        if cid not in cut:
            cut[cid] = knock_out(load_crop(cid))

    made = 0
    for slot, spec in sorted(assign.items()):
        img = cut[spec["crop"]]
        if spec.get("mirror"):
            img = img.transpose(Image.FLIP_LEFT_RIGHT)
        # per-slot target height: source sheets drew the bird at wildly
        # different sizes, so normalize each pose by its own body height
        target_h = spec.get("h", 158)
        k = target_h / img.height
        k = min(k, (CELL_W - 6) / img.width, (CELL_H - 6) / img.height)
        sw, sh = max(1, round(img.width * k)), max(1, round(img.height * k))
        img = img.resize((sw, sh), Image.LANCZOS)
        cell = Image.new("RGBA", (CELL_W, CELL_H), (0, 0, 0, 0))
        cell.paste(img, ((CELL_W - sw) // 2, CELL_H - BOTTOM_PAD - sh), img)
        state, frame = slot.split("/")
        d = os.path.join(OUT, state)
        os.makedirs(d, exist_ok=True)
        cell.save(os.path.join(d, f"{frame}.png"))
        made += 1
    print(f"wrote {made} cells (scale {k:.3f}) -> {OUT}")


if __name__ == "__main__":
    main(sys.argv[1])
