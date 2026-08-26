#!/usr/bin/env python3
"""Render a QA contact sheet for a Codex-compatible v2 pet atlas.

Draws the 8x11 sprite grid on a dark checkerboard (so transparency is
visible), overlays thin cell borders, and annotates row labels (state
names) and column indices. The output is a single PNG that a human or a
vision model can inspect to verify frame placement, alignment, and
transparency at a glance.

The v2 atlas contract: 1536x2288 RGBA, 8 columns x 11 rows of 192x208
cells. Rows 0-8 are animation states, rows 9-10 are the 16
look-direction cells. An atlas of a different size is still rendered
(cropped or padded to the grid) with a warning in the summary, so a
malformed asset can be inspected rather than rejected blind.

Usage:
    python make_contact_sheet.py ATLAS [--output OUT.png]

Requires Pillow. Never raises on bad input: errors are printed as a
one-line summary and the exit code is 1.
"""

import argparse
import os
import sys

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - environment guard
    print("error: Pillow is required (pip install Pillow)")
    sys.exit(1)

COLS = 8
ROWS = 11
CELL_W = 192
CELL_H = 208
ATLAS_W = COLS * CELL_W    # 1536
ATLAS_H = ROWS * CELL_H    # 2288

ROW_LABELS = [
    "idle",
    "running-right",
    "running-left",
    "waving",
    "jumping",
    "failed",
    "waiting",
    "running",
    "review",
    "look A",
    "look B",
]

CHECKER_DARK = (0x22, 0x22, 0x22)
CHECKER_LIGHT = (0x2A, 0x2A, 0x2A)
CHECKER_SIZE = 16
MARGIN_BG = (0x14, 0x14, 0x14)
GRID_LINE = (0x50, 0x50, 0x50)
TEXT_COLOR = (0xD0, 0xD0, 0xD0)

LEFT_MARGIN = 120   # room for row labels
TOP_MARGIN = 28     # room for column indices


def draw_checkerboard(draw, x0, y0, width, height):
    """Fill the rect at (x0, y0) with the two-tone transparency checker."""
    for ty in range(0, height, CHECKER_SIZE):
        for tx in range(0, width, CHECKER_SIZE):
            color = (
                CHECKER_LIGHT
                if ((tx // CHECKER_SIZE) + (ty // CHECKER_SIZE)) % 2
                else CHECKER_DARK
            )
            draw.rectangle(
                [
                    x0 + tx,
                    y0 + ty,
                    x0 + min(tx + CHECKER_SIZE, width) - 1,
                    y0 + min(ty + CHECKER_SIZE, height) - 1,
                ],
                fill=color,
            )


def build_sheet(atlas):
    """Compose the annotated contact sheet and return it as an RGB image."""
    sheet_w = LEFT_MARGIN + ATLAS_W + 1
    sheet_h = TOP_MARGIN + ATLAS_H + 1
    sheet = Image.new("RGB", (sheet_w, sheet_h), MARGIN_BG)
    draw = ImageDraw.Draw(sheet)

    draw_checkerboard(draw, LEFT_MARGIN, TOP_MARGIN, ATLAS_W, ATLAS_H)

    # Composite the atlas over the checker using its own alpha.
    sprite = atlas.convert("RGBA")
    if sprite.size != (ATLAS_W, ATLAS_H):
        canvas = Image.new("RGBA", (ATLAS_W, ATLAS_H), (0, 0, 0, 0))
        canvas.paste(sprite.crop((0, 0, min(sprite.width, ATLAS_W),
                                  min(sprite.height, ATLAS_H))), (0, 0))
        sprite = canvas
    sheet.paste(sprite, (LEFT_MARGIN, TOP_MARGIN), sprite)

    # Thin grid lines on cell boundaries.
    for c in range(COLS + 1):
        x = LEFT_MARGIN + c * CELL_W
        draw.line([(x, TOP_MARGIN), (x, TOP_MARGIN + ATLAS_H)], fill=GRID_LINE)
    for r in range(ROWS + 1):
        y = TOP_MARGIN + r * CELL_H
        draw.line([(LEFT_MARGIN, y), (LEFT_MARGIN + ATLAS_W, y)], fill=GRID_LINE)

    font = ImageFont.load_default()

    # Column indices, centered over each column.
    for c in range(COLS):
        text = str(c)
        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x = LEFT_MARGIN + c * CELL_W + (CELL_W - tw) // 2
        draw.text((x, (TOP_MARGIN - th) // 2), text, fill=TEXT_COLOR, font=font)

    # Row labels, right-aligned and vertically centered per row.
    for r, label in enumerate(ROW_LABELS):
        bbox = draw.textbbox((0, 0), label, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x = LEFT_MARGIN - 8 - tw
        y = TOP_MARGIN + r * CELL_H + (CELL_H - th) // 2
        draw.text((x, y), label, fill=TEXT_COLOR, font=font)

    return sheet


def main():
    parser = argparse.ArgumentParser(
        description="Render a v2 pet atlas as an annotated QA contact sheet."
    )
    parser.add_argument("atlas", help="path to the atlas image (PNG or WebP)")
    parser.add_argument(
        "--output",
        help="output PNG path (default: <atlas>_contact.png next to the atlas)",
    )
    args = parser.parse_args()

    out_path = args.output or (
        os.path.splitext(args.atlas)[0] + "_contact.png"
    )

    try:
        with Image.open(args.atlas) as atlas:
            atlas.load()
            atlas_size = atlas.size
            sheet = build_sheet(atlas)
    except FileNotFoundError:
        print(f"error: atlas not found: {args.atlas}")
        return 1
    except Exception as exc:
        print(f"error: could not render {args.atlas}: {exc}")
        return 1

    try:
        out_dir = os.path.dirname(os.path.abspath(out_path))
        os.makedirs(out_dir, exist_ok=True)
        sheet.save(out_path, "PNG")
    except Exception as exc:
        print(f"error: could not write {out_path}: {exc}")
        return 1

    warn = (
        ""
        if atlas_size == (ATLAS_W, ATLAS_H)
        else f" WARNING: atlas is {atlas_size[0]}x{atlas_size[1]}, expected {ATLAS_W}x{ATLAS_H}"
    )
    size_bytes = os.path.getsize(out_path)
    print(
        f"contact sheet: {COLS}x{ROWS} grid from {os.path.basename(args.atlas)} "
        f"({atlas_size[0]}x{atlas_size[1]}) -> {out_path} "
        f"({sheet.width}x{sheet.height}, {size_bytes} bytes){warn}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
