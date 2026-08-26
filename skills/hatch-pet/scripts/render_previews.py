#!/usr/bin/env python3
"""Render looping preview GIFs from a Codex-compatible v2 pet atlas.

The v2 atlas is a 1536x2288 RGBA image laid out as 8 columns x 11 rows of
192x208 cells.  Rows 0-8 hold the standard animation states; rows 9-10 hold
the 16 look-direction cells (22.5 degree steps, clockwise, 000 = up).

For every standard state this script extracts the state's used frames and
writes a looping animated GIF with the contract's per-frame durations.  GIF
has no alpha channel, so each RGBA frame is matted onto a solid #1a1a1e
background.  A final look.gif cycles the 16 look cells clockwise (increasing
heading, row 9 then row 10) at 200 ms per frame.

Usage:
    python3 render_previews.py ATLAS --output-dir DIR

Exits 0 on success, 1 on any error.  Errors are reported as a one-line
message; bad input never produces a traceback.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:  # pragma: no cover - environment guard
    print("ERROR: Pillow is required (pip install Pillow)")
    sys.exit(1)

CELL_W, CELL_H = 192, 208
COLS = 8
ATLAS_W, ATLAS_H = 1536, 2288
BACKGROUND = (0x1A, 0x1A, 0x1E, 0xFF)

# (state name, atlas row, per-frame durations in ms).  The number of
# durations equals the number of used columns, which start at column 0.
STATES = (
    ("idle", 0, (280, 110, 110, 140, 140, 320)),
    ("running-right", 1, (120,) * 7 + (220,)),
    ("running-left", 2, (120,) * 7 + (220,)),
    ("waving", 3, (140,) * 3 + (280,)),
    ("jumping", 4, (140,) * 4 + (280,)),
    ("failed", 5, (140,) * 7 + (240,)),
    ("waiting", 6, (150,) * 5 + (260,)),
    ("running", 7, (120,) * 5 + (220,)),
    ("review", 8, (150,) * 5 + (280,)),
)

LOOK_ROWS = (9, 10)  # row 9: 000-157.5 deg, row 10: 180-337.5 deg
LOOK_FRAME_MS = 200


def extract_cell(atlas: Image.Image, row: int, col: int) -> Image.Image:
    """Return one cell matted onto the opaque preview background, as RGB."""
    x, y = col * CELL_W, row * CELL_H
    frame = Image.new("RGBA", (CELL_W, CELL_H), BACKGROUND)
    frame.alpha_composite(atlas.crop((x, y, x + CELL_W, y + CELL_H)))
    return frame.convert("RGB")


def write_gif(path: Path, frames: list[Image.Image], durations: list[int]) -> None:
    """Write an infinitely looping GIF with explicit per-frame durations (ms)."""
    frames[0].save(
        path,
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render looping preview GIFs from a v2 pet atlas."
    )
    parser.add_argument("atlas", help="path to the atlas (PNG or WebP, 1536x2288 RGBA)")
    parser.add_argument(
        "--output-dir", required=True, help="directory to write the GIFs into"
    )
    args = parser.parse_args()

    atlas_path = Path(args.atlas)
    out_dir = Path(args.output_dir)

    try:
        with Image.open(atlas_path) as img:
            atlas = img.convert("RGBA")
    except (OSError, ValueError) as exc:
        print(f"ERROR: cannot open atlas {atlas_path}: {exc}")
        return 1

    if atlas.size != (ATLAS_W, ATLAS_H):
        print(
            f"ERROR: atlas is {atlas.size[0]}x{atlas.size[1]}, "
            f"expected {ATLAS_W}x{ATLAS_H}"
        )
        return 1

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        written = []

        for name, row, durations in STATES:
            frames = [extract_cell(atlas, row, col) for col in range(len(durations))]
            gif_path = out_dir / f"{name}.gif"
            write_gif(gif_path, frames, list(durations))
            written.append(gif_path)

        look_frames = [
            extract_cell(atlas, row, col) for row in LOOK_ROWS for col in range(COLS)
        ]
        look_path = out_dir / "look.gif"
        write_gif(look_path, look_frames, [LOOK_FRAME_MS] * len(look_frames))
        written.append(look_path)
    except OSError as exc:
        print(f"ERROR: failed writing previews: {exc}")
        return 1

    total = sum(p.stat().st_size for p in written)
    print(
        f"Wrote {len(written)} preview GIFs ({len(STATES)} states + look) "
        f"to {out_dir} ({total} bytes total)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
