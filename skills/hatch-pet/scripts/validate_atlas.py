#!/usr/bin/env python3
"""Validate a v2 pet sprite atlas against the interop format contract.

Contract (v2):
  - Atlas image is PNG or WebP, exactly 1536x2288 pixels, RGBA, arranged as
    8 columns x 11 rows of 192x208 cells on a transparent background.
  - Rows 0-8 are standard animation states. Each row uses a fixed number of
    leading columns (see STANDARD_ROWS); every used cell must contain at
    least one pixel with alpha > 0, and every cell after the last used
    column must be fully transparent. Exception: real-world v2 sheets (the
    Codex reference asset) may carry an optional 7th idle frame in row 0
    cell 6; it is accepted and reported as a warning, never an error.
  - Rows 9-10 are look-direction rows (16 headings, 22.5 degrees apart,
    000 = up). All 8 cells of both rows must be non-empty.

Usage:
  python validate_atlas.py ATLAS [--json-out REPORT.json]

Prints a one-line summary to stdout and optionally writes a JSON report:
  {"ok": bool, "errors": [...], "warnings": [...], "rows": {...}}

Exit codes: 0 = atlas valid, 1 = atlas invalid or unreadable.
The script never raises on bad input; problems are reported in the JSON.
"""

import argparse
import json
import sys

ATLAS_WIDTH = 1536
ATLAS_HEIGHT = 2288
COLUMNS = 8
ROWS = 11
CELL_WIDTH = 192
CELL_HEIGHT = 208

# Row index -> (state name, required used columns, optional extra columns).
# Optional cells may be populated (warning) or empty; the Codex reference
# asset ships a 7th idle frame, so idle allows one optional cell.
STANDARD_ROWS = [
    ("idle", 6, 1),
    ("running-right", 8, 0),
    ("running-left", 8, 0),
    ("waving", 4, 0),
    ("jumping", 5, 0),
    ("failed", 8, 0),
    ("waiting", 6, 0),
    ("running", 6, 0),
    ("review", 6, 0),
]

# Row index -> look-row name (all 8 cells used in each).
LOOK_ROWS = [
    (9, "look-000-157"),
    (10, "look-180-337"),
]


def cell_has_pixels(image, col, row):
    """Return True if any pixel in cell (col, row) has alpha > 0."""
    box = (
        col * CELL_WIDTH,
        row * CELL_HEIGHT,
        (col + 1) * CELL_WIDTH,
        (row + 1) * CELL_HEIGHT,
    )
    alpha = image.crop(box).getchannel("A")
    _, max_alpha = alpha.getextrema()
    return max_alpha > 0


def validate(atlas_path):
    """Validate the atlas at atlas_path; return the report dict."""
    report = {"ok": False, "errors": [], "warnings": [], "rows": {}}
    errors = report["errors"]
    warnings = report["warnings"]

    try:
        from PIL import Image
    except ImportError:
        errors.append("Pillow is not installed (pip install Pillow)")
        return report

    try:
        with Image.open(atlas_path) as opened:
            opened.load()
            image = opened
            image_format = opened.format
    except FileNotFoundError:
        errors.append("atlas file not found: %s" % atlas_path)
        return report
    except Exception as exc:  # decode errors, permission errors, etc.
        errors.append("cannot read atlas as an image: %s" % exc)
        return report

    if image_format not in ("PNG", "WEBP"):
        warnings.append(
            "atlas format is %s; contract expects PNG or WebP" % image_format
        )

    if image.size != (ATLAS_WIDTH, ATLAS_HEIGHT):
        errors.append(
            "atlas is %dx%d; contract requires exactly %dx%d"
            % (image.width, image.height, ATLAS_WIDTH, ATLAS_HEIGHT)
        )
        # Cell geometry is undefined at the wrong size; stop here.
        return report

    if image.mode != "RGBA":
        source_mode = image.mode
        try:
            image = image.convert("RGBA")
            warnings.append(
                "atlas mode is %s, not RGBA; converted for validation" % source_mode
            )
        except Exception as exc:
            errors.append("atlas is not readable as RGBA: %s" % exc)
            return report

    for row_index, (name, used, optional) in enumerate(STANDARD_ROWS):
        nonempty = [cell_has_pixels(image, col, row_index) for col in range(COLUMNS)]
        row_ok = True
        for col in range(used):
            if not nonempty[col]:
                errors.append(
                    "row %d (%s): used cell %d is empty (all alpha 0)"
                    % (row_index, name, col)
                )
                row_ok = False
        for col in range(used, used + optional):
            if nonempty[col]:
                warnings.append(
                    "row %d (%s): optional cell %d is populated (extra frame)"
                    % (row_index, name, col)
                )
        for col in range(used + optional, COLUMNS):
            if nonempty[col]:
                errors.append(
                    "row %d (%s): cell %d must be fully transparent "
                    "(after last used column %d)" % (row_index, name, col, used - 1)
                )
                row_ok = False
        report["rows"][name] = {
            "row": row_index,
            "used_columns": used,
            "optional_columns": optional,
            "nonempty": nonempty,
            "ok": row_ok,
        }

    for row_index, name in LOOK_ROWS:
        nonempty = [cell_has_pixels(image, col, row_index) for col in range(COLUMNS)]
        row_ok = True
        for col in range(COLUMNS):
            if not nonempty[col]:
                errors.append(
                    "row %d (%s): look cell %d is empty (all alpha 0)"
                    % (row_index, name, col)
                )
                row_ok = False
        report["rows"][name] = {
            "row": row_index,
            "used_columns": COLUMNS,
            "optional_columns": 0,
            "nonempty": nonempty,
            "ok": row_ok,
        }

    report["ok"] = not errors
    return report


def main():
    parser = argparse.ArgumentParser(
        description="Validate a v2 pet sprite atlas (1536x2288, 8x11 grid)."
    )
    parser.add_argument("atlas", help="path to the atlas image (PNG or WebP)")
    parser.add_argument("--json-out", help="write the full JSON report to this path")
    args = parser.parse_args()

    report = validate(args.atlas)

    if args.json_out:
        try:
            with open(args.json_out, "w", encoding="utf-8") as handle:
                json.dump(report, handle, indent=2)
        except OSError as exc:
            report["ok"] = False
            report["errors"].append("cannot write JSON report: %s" % exc)

    if report["ok"]:
        rows_ok = sum(1 for row in report["rows"].values() if row["ok"])
        print(
            "OK: %s valid v2 atlas (%d/%d rows pass, %d warning(s))"
            % (args.atlas, rows_ok, ROWS, len(report["warnings"]))
        )
    else:
        first = report["errors"][0] if report["errors"] else "unknown error"
        print(
            "FAIL: %s - %d error(s); first: %s"
            % (args.atlas, len(report["errors"]), first)
        )

    sys.exit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
