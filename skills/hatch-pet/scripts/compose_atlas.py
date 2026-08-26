#!/usr/bin/env python3
"""Compose a Codex-compatible v2 pet atlas from split_atlas.py output.

Reassembles the exact 1536x2288 RGBA atlas (8 columns x 11 rows of 192x208
cells, transparent background) from a frames directory produced by the
sibling split_atlas.py:

    frames-root/
        frames-manifest.json       written by split_atlas.py (optional here)
        <state>/00.png .. NN.png   rows 0-8, one directory per state
        look/000.png, 022.5.png,   16 look-direction cells for rows 9-10,
        ... 337.5.png              named by angle in degrees (000 = up)

When frames-manifest.json is present each listed file is pasted at its
recorded row/col, which reproduces the source atlas exactly. Without a
manifest the directory layout is scanned instead: state rows are filled in
contract order (idle, running-right, running-left, waving, jumping, failed,
waiting, running, review) at columns 0..n-1 from the files actually present,
and the 16 look cells fill rows 9-10 in ascending degree order. Frames are
ordered by the integer groups in their filenames, so 00.png, frame_03.png,
and 022.5.png all sort correctly. Nominal v2 frame counts are checked but
deviations are only warnings — real assets deviate (Axi ships 7 idle frames).

Outputs a PNG (--output) and optionally a lossless WebP (--webp-output,
saved with exact=True so RGB values under transparent pixels survive).
--verify-against compares the composition pixel-for-pixel against a
reference atlas via ImageChops.difference on RGBA. --json-out writes a
machine-readable report. Bad input never raises: problems are collected
into the report and the summary line, and the exit code is 1.
"""

import argparse
import json
import re
import sys
from pathlib import Path

from PIL import Image, ImageChops

ATLAS_SIZE = (1536, 2288)
CELL_W, CELL_H = 192, 208
COLUMNS, ROWS = 8, 11
MANIFEST_NAME = "frames-manifest.json"

#: Standard states in row order (row index = position) with nominal v2 counts.
STATE_ROWS = [
    ("idle", 6),
    ("running-right", 8),
    ("running-left", 8),
    ("waving", 4),
    ("jumping", 5),
    ("failed", 8),
    ("waiting", 6),
    ("running", 6),
    ("review", 6),
]

LOOK_ROW_START = 9
LOOK_CELLS = 16

_INT_GROUPS = re.compile(r"\d+")


def numeric_key(path):
    """Sort key: tuple of the integer groups in the stem, then the stem.

    Orders 00.png, frame_03.png, and angle names like 022.5.png (stem
    "022.5" keys as (22, 5), which stays in degree order) alike.
    """
    return tuple(int(g) for g in _INT_GROUPS.findall(path.stem)), path.stem


def placements_from_manifest(frames_root, manifest_path, errors, warnings):
    """Read (path, row, col, state) placements from frames-manifest.json.

    Returns None when the manifest is unusable, so the caller can fall back
    to scanning the directory layout.
    """
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        files = manifest["files"]
    except (OSError, ValueError, KeyError) as exc:
        warnings.append(f"{manifest_path}: unusable manifest ({exc}); scanning layout")
        return None

    placements, claimed = [], {}
    for entry in files:
        try:
            rel, row, col = entry["file"], int(entry["row"]), int(entry["col"])
        except (TypeError, KeyError, ValueError):
            errors.append(f"manifest entry missing file/row/col: {entry!r}")
            continue
        if not (0 <= row < ROWS and 0 <= col < COLUMNS):
            errors.append(f"{rel}: cell ({row},{col}) outside the 8x11 grid")
            continue
        if (row, col) in claimed:
            errors.append(f"{rel}: cell ({row},{col}) already used by {claimed[row, col]}")
            continue
        claimed[row, col] = rel
        placements.append((frames_root / rel, row, col, entry.get("state", "?")))
    return placements


def placements_from_layout(frames_root, errors, warnings):
    """Build placements by scanning the <state>/ and look/ directories."""
    placements = []
    for row, (state, nominal) in enumerate(STATE_ROWS):
        state_dir = frames_root / state
        if not state_dir.is_dir():
            errors.append(f"missing state directory: {state_dir}")
            continue
        paths = sorted(state_dir.glob("*.png"), key=numeric_key)
        if not paths:
            errors.append(f"{state_dir}: no PNG frames found")
        elif len(paths) > COLUMNS:
            errors.append(f"{state_dir}: {len(paths)} frames exceed {COLUMNS} columns")
        else:
            if len(paths) != nominal:
                warnings.append(f"{state}: {len(paths)} frames (nominal v2 count is {nominal})")
            placements += [(p, row, col, state) for col, p in enumerate(paths)]

    look_dir = frames_root / "look"
    if not look_dir.is_dir():
        errors.append(f"missing look directory: {look_dir}")
    else:
        paths = sorted(look_dir.glob("*.png"), key=numeric_key)
        if len(paths) != LOOK_CELLS:
            errors.append(f"{look_dir}: found {len(paths)} PNGs, expected {LOOK_CELLS}")
        else:
            placements += [
                (p, LOOK_ROW_START + i // COLUMNS, i % COLUMNS, "look")
                for i, p in enumerate(paths)
            ]
    return placements


def compose(frames_root, errors, warnings, placed):
    """Paste every frame onto a transparent atlas canvas."""
    manifest_path = frames_root / MANIFEST_NAME
    placements = None
    if manifest_path.is_file():
        placements = placements_from_manifest(frames_root, manifest_path, errors, warnings)
    if placements is None:
        placements = placements_from_layout(frames_root, errors, warnings)

    atlas = Image.new("RGBA", ATLAS_SIZE, (0, 0, 0, 0))
    for path, row, col, state in placements:
        try:
            with Image.open(path) as im:
                im.load()
                frame = im.convert("RGBA")
        except OSError as exc:
            errors.append(f"{path}: unreadable image ({exc})")
            continue
        if frame.size != (CELL_W, CELL_H):
            errors.append(
                f"{path}: frame is {frame.size[0]}x{frame.size[1]}, "
                f"expected {CELL_W}x{CELL_H}"
            )
            continue
        atlas.paste(frame, (col * CELL_W, row * CELL_H))
        placed[state] = placed.get(state, 0) + 1
    return atlas


def verify(atlas, reference_path, errors):
    """Compare atlas to a reference image; return True when identical."""
    try:
        with Image.open(reference_path) as im:
            im.load()
            reference = im.convert("RGBA")
    except OSError as exc:
        errors.append(f"{reference_path}: unreadable reference ({exc})")
        return False
    if reference.size != atlas.size:
        errors.append(f"verify: reference is {reference.size}, atlas is {atlas.size}")
        return False
    bbox = ImageChops.difference(atlas, reference).getbbox()
    if bbox is not None:
        errors.append(f"verify: pixels differ from {reference_path} in bbox {bbox}")
        return False
    return True


def save_outputs(atlas, output, webp_output, errors):
    """Write the PNG (and optional lossless WebP); record failures."""
    written = []
    for path, kwargs in [
        (output, {"format": "PNG"}),
        (webp_output, {"format": "WEBP", "lossless": True, "exact": True}),
    ]:
        if path is None:
            continue
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            atlas.save(path, **kwargs)
            written.append(str(path))
        except OSError as exc:
            errors.append(f"{path}: write failed ({exc})")
    return written


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Compose a v2 pet atlas from split_atlas.py output."
    )
    parser.add_argument(
        "--frames-root", required=True, type=Path,
        help="Directory produced by split_atlas.py.",
    )
    parser.add_argument(
        "--output", required=True, type=Path, help="Composed atlas PNG path."
    )
    parser.add_argument(
        "--webp-output", type=Path,
        help="Also write a lossless WebP copy of the atlas.",
    )
    parser.add_argument(
        "--verify-against", type=Path,
        help="Reference atlas; fail unless the composition matches it exactly.",
    )
    parser.add_argument(
        "--json-out", type=Path, help="Write a JSON report to this path."
    )
    args = parser.parse_args(argv)

    errors, warnings, placed = [], [], {}
    if args.frames_root.is_dir():
        atlas = compose(args.frames_root, errors, warnings, placed)
    else:
        atlas = None
        errors.append(f"frames root is not a directory: {args.frames_root}")

    verified = None
    written = []
    if atlas is not None and not errors:
        if args.verify_against:
            verified = verify(atlas, args.verify_against, errors)
        written = save_outputs(atlas, args.output, args.webp_output, errors)

    ok = not errors
    report = {
        "ok": ok,
        "frames_root": str(args.frames_root),
        "atlas_size": list(ATLAS_SIZE),
        "frames_placed": placed,
        "outputs": written,
        "verified_against": str(args.verify_against) if args.verify_against else None,
        "round_trip_identical": verified,
        "warnings": warnings,
        "errors": errors,
    }
    if args.json_out:
        try:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        except OSError as exc:
            print(f"compose_atlas: FAILED to write report {args.json_out}: {exc}")
            return 1

    total = sum(placed.values())
    if ok:
        vnote = ", round-trip identical" if verified else ""
        print(
            f"compose_atlas: OK {total} frames -> {args.output}"
            f" ({len(written)} file(s) written{vnote}; {len(warnings)} warning(s))"
        )
    else:
        print(f"compose_atlas: FAILED with {len(errors)} error(s): {errors[0]}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
