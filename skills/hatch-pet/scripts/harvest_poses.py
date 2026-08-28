"""Harvest every distinct pose from the generated Huma sheets into a crop
library, then render contact grids for vision review."""
import json
import os
import sys
from collections import deque
from PIL import Image

# the pet being hatched; pass its name so this is not wired to one pet
RUN = os.path.expanduser(os.environ.get("HATCH_RUN")
                         or r"~/.claude/pets/.hatch/" + (sys.argv[1] if len(sys.argv) > 1 else "pet"))
DECODED = os.path.join(RUN, "decoded")
LIB = os.path.join(RUN, "library")
GRIDS = os.path.join(RUN, "library-grids")
MIN_AREA = 1200
MIN_DIM = 28
PAD = 5
TOL = 55  # RGB euclidean distance from background


def bg_color(im):
    w, h = im.size
    pts = [im.getpixel(p) for p in
           [(2, 2), (w - 3, 2), (2, h - 3), (w - 3, h - 3), (w // 2, 2), (w // 2, h - 3)]]
    return tuple(sorted(c[i] for c in pts)[len(pts) // 2] for i in range(3))


def components(mask, w, h):
    seen = bytearray(w * h)
    out = []
    for start in range(w * h):
        if mask[start] and not seen[start]:
            q = deque([start])
            seen[start] = 1
            xs_min = ys_min = 1 << 30
            xs_max = ys_max = -1
            area = 0
            while q:
                idx = q.popleft()
                x, y = idx % w, idx // w
                area += 1
                if x < xs_min: xs_min = x
                if x > xs_max: xs_max = x
                if y < ys_min: ys_min = y
                if y > ys_max: ys_max = y
                for nx, ny in ((x-1, y), (x+1, y), (x, y-1), (x, y+1),
                               (x-1, y-1), (x+1, y-1), (x-1, y+1), (x+1, y+1)):
                    if 0 <= nx < w and 0 <= ny < h:
                        n = ny * w + nx
                        if mask[n] and not seen[n]:
                            seen[n] = 1
                            q.append(n)
            out.append((xs_min, ys_min, xs_max, ys_max, area))
    return out


def main():
    os.makedirs(LIB, exist_ok=True)
    os.makedirs(GRIDS, exist_ok=True)
    index = []
    for name in sorted(os.listdir(DECODED)):
        if not name.endswith(".png") or name == "base.png":
            continue
        sheet = os.path.splitext(name)[0]
        im = Image.open(os.path.join(DECODED, name)).convert("RGB")
        w, h = im.size
        bg = bg_color(im)
        px = im.load()
        mask = bytearray(w * h)
        for y in range(h):
            row = y * w
            for x in range(w):
                r, g, b = px[x, y]
                d2 = (r - bg[0]) ** 2 + (g - bg[1]) ** 2 + (b - bg[2]) ** 2
                if d2 > TOL * TOL:
                    mask[row + x] = 1
        comps = [c for c in components(mask, w, h)
                 if c[4] >= MIN_AREA and (c[2] - c[0]) >= MIN_DIM and (c[3] - c[1]) >= MIN_DIM]
        comps.sort(key=lambda c: (c[1] // 100, c[0]))  # row-major reading order
        outdir = os.path.join(LIB, sheet)
        os.makedirs(outdir, exist_ok=True)
        for i, (x0, y0, x1, y1, area) in enumerate(comps):
            crop = im.crop((max(0, x0 - PAD), max(0, y0 - PAD),
                            min(w, x1 + PAD), min(h, y1 + PAD)))
            cid = f"{sheet}-{i:02d}"
            crop.save(os.path.join(outdir, f"{i:02d}.png"))
            index.append({"id": cid, "sheet": sheet, "n": i,
                          "w": crop.width, "h": crop.height, "area": area})
        print(f"{sheet}: {len(comps)} poses")

    json.dump(index, open(os.path.join(LIB, "index.json"), "w"), indent=1)

    # contact grids per sheet (labeled, ~8 cols) for vision review
    for sheet in sorted({e["sheet"] for e in index}):
        entries = [e for e in index if e["sheet"] == sheet]
        cols = 8
        cell = 150
        rows = (len(entries) + cols - 1) // cols
        grid = Image.new("RGB", (cols * cell, rows * (cell + 16)), (18, 18, 22))
        from PIL import ImageDraw
        d = ImageDraw.Draw(grid)
        for k, e in enumerate(entries):
            im = Image.open(os.path.join(LIB, sheet, f"{e['n']:02d}.png"))
            im.thumbnail((cell - 8, cell - 8))
            gx = (k % cols) * cell
            gy = (k // cols) * (cell + 16)
            grid.paste(im, (gx + (cell - im.width) // 2, gy + (cell - im.height) // 2))
            d.text((gx + 4, gy + cell + 2), e["id"], fill=(200, 200, 200))
        grid.save(os.path.join(GRIDS, f"{sheet}.png"))
    print(f"library: {len(index)} crops, grids in {GRIDS}")


if __name__ == "__main__":
    main()
