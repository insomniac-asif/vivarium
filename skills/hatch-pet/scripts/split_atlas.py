#!/usr/bin/env python3
"""Slice a v2 pet atlas into per-state frame PNGs.

The v2 atlas contract is a 1536x2288 RGBA sheet arranged as an 8x11 grid of
192x208 cells. Rows 0-8 hold the standard animation states (only the leading
"used" columns of each row carry frames); rows 9-10 hold 16 look-direction
cells covering 360 degrees in 22.5-degree steps (000 = up).

For each standard state this script writes <out>/<state>/00.png .. NN.png,
and for the look rows <out>/look/<angle>.png (000.png, 022.5.png, ...,
337.5.png). It also writes <out>/frames-manifest.json listing every emitted
file with its source row/col plus the contract frame duration.

Usage:
    python split_atlas.py ATLAS --output-dir DIR [--json-out REPORT]

The script never raises on bad input: problems are printed as a one-line
summary and recorded in the optional --json-out report, with exit code 1.
"""

import argparse
import json
import sys
from pathlib import Path

from PIL import Image

ATLAS_SIZE = (1536, 2288)
CELL_W, CELL_H = 192, 208
GRID_COLS, GRID_ROWS = 8, 11

# Standard states, in row order (row index = position in this list).
# Each entry: (state name, per-frame durations in ms); frame count == number
# of used columns in that row.
STATES = [
    ("idle", [280, 110, 110, 140, 140, 320]),
    ("running-right", [120] * 7 + [220]),
    ("running-left", [120] * 7 + [220]),
    ("waving", [140] * 3 + [280]),
    ("jumping", [140] * 4 + [280]),
    ("failed", [140] * 7 + [240]),
    ("waiting", [150] * 5 + [260]),
    ("running", [120] * 5 + [220]),
    ("review", [150] * 5 + [280]),
]

LOOK_ROWS = (9, 10)
LOOK_STEP_DEG = 22.5  # 16 cells x 22.5 = 360; angle 0 = up.

# Real v2 sheets may carry optional extra frames past a row's nominal count
# (the Codex Axi asset ships a 7th idle frame). Populated optional cells are
# extracted too so compose_atlas.py can rebuild the sheet losslessly; the
# contract defines no duration for them (durationMs is null in the manifest).
OPTIONAL_EXTRAS = {"idle": 1}


def angle_name(angle: float) -> str:
    """Filename stem for a look angle: 000, 022.5, 045, ..., 337.5."""
    if angle == int(angle):
        return f"{int(angle):03d}"
    whole, frac = divmod(angle, 1)
    return f"{int(whole):03d}.{int(round(frac * 10))}"


def cell_box(col: int, row: int):
    """Pixel box (left, upper, right, lower) of a grid cell."""
    left, upper = col * CELL_W, row * CELL_H
    return (left, upper, left + CELL_W, upper + CELL_H)


def split_atlas(atlas_path: Path, out_dir: Path) -> dict:
    """Slice the atlas; return the manifest dict. Raises ValueError on bad input."""
    with Image.open(atlas_path) as img:
        if img.size != ATLAS_SIZE:
            raise ValueError(
                f"atlas is {img.size[0]}x{img.size[1]}, contract requires "
                f"{ATLAS_SIZE[0]}x{ATLAS_SIZE[1]}"
            )
        atlas = img.convert("RGBA")

    out_dir.mkdir(parents=True, exist_ok=True)
    files = []

    for row, (state, durations) in enumerate(STATES):
        state_dir = out_dir / state
        state_dir.mkdir(exist_ok=True)
        for col, duration in enumerate(durations):
            rel = f"{state}/{col:02d}.png"
            atlas.crop(cell_box(col, row)).save(state_dir / f"{col:02d}.png")
            files.append(
                {"file": rel, "state": state, "row": row, "col": col,
                 "frame": col, "durationMs": duration}
            )
        used = len(durations)
        for col in range(used, used + OPTIONAL_EXTRAS.get(state, 0)):
            cell = atlas.crop(cell_box(col, row))
            if cell.getchannel("A").getextrema()[1] == 0:
                continue  # optional cell is empty; nothing to extract
            rel = f"{state}/{col:02d}.png"
            cell.save(state_dir / f"{col:02d}.png")
            files.append(
                {"file": rel, "state": state, "row": row, "col": col,
                 "frame": col, "durationMs": None, "optional": True}
            )

    look_dir = out_dir / "look"
    look_dir.mkdir(exist_ok=True)
    for i in range(GRID_COLS * len(LOOK_ROWS)):
        row = LOOK_ROWS[i // GRID_COLS]
        col = i % GRID_COLS
        angle = i * LOOK_STEP_DEG
        rel = f"look/{angle_name(angle)}.png"
        atlas.crop(cell_box(col, row)).save(out_dir / rel)
        files.append(
            {"file": rel, "state": "look", "row": row, "col": col,
             "angleDeg": angle}
        )

    manifest = {
        "atlas": str(atlas_path),
        "cellSize": [CELL_W, CELL_H],
        "grid": [GRID_COLS, GRID_ROWS],
        "fileCount": len(files),
        "files": files,
    }
    manifest_path = out_dir / "frames-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Slice a v2 pet atlas into per-state frame PNGs."
    )
    parser.add_argument("atlas", type=Path, help="path to the v2 atlas (PNG or WebP)")
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="directory to write frame PNGs and frames-manifest.json")
    parser.add_argument("--json-out", type=Path, default=None,
                        help="optional path for a machine-readable run report")
    args = parser.parse_args()

    report = {"ok": False, "atlas": str(args.atlas),
              "outputDir": str(args.output_dir), "errors": []}
    try:
        manifest = split_atlas(args.atlas, args.output_dir)
        report["ok"] = True
        report["fileCount"] = manifest["fileCount"]
        summary = (
            f"OK: wrote {manifest['fileCount']} frames + frames-manifest.json "
            f"to {args.output_dir}"
        )
    except (OSError, ValueError) as exc:
        report["errors"].append(str(exc))
        summary = f"ERROR: {exc}"

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(summary)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
