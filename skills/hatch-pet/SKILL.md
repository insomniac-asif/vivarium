---
name: hatch-pet
description: Hatch a new Codex-compatible v2 animated pet for the Vivarium overlay (and Codex CLI) from a concept, reference image, or brand cue. Use for any new pet, mascot, pet repair, or spritesheet packaging request. Generates art with whatever image-generation tool is available, then deterministically slices, assembles, validates, and packages the 8x11 atlas.
---

# Hatch Pet

Create a v2 animated pet: `pet.json` + a 1536×2288 spritesheet (8×11 grid of
192×208 cells). Read `docs/format.md` in the plugin root first — it defines
the row layout, used columns, frame durations, and look-direction semantics.
The scripts referenced below live in `skills/hatch-pet/scripts/` and need
Python 3 + Pillow.

## Inputs

All optional: pet name, description, style, reference images. Infer what is
missing. If no image-generation tool is connected (check for MCP tools that
generate images), tell the user hatching needs one — or accept user-provided
row strips / an existing atlas instead.

## Pipeline

1. **Base identity.** Generate (or accept) one full-body reference of the pet
   on a flat chroma background (pick magenta `#ff00ff` unless the pet is
   pink/red — then green `#00ff00`). Compact silhouette that stays readable at
   192×208; no text, no scenery, no detached effects, no drop shadows.

2. **Standard rows (0–8).** For each state — idle(6), running-right(8),
   running-left(8), waving(4), jumping(5), failed(8), waiting(6), running(6),
   review(6) — generate ONE horizontal strip with exactly that many poses,
   same chroma background, grounded on the base reference for identity.
   State semantics (keep motion in the body, never in floating effects):
   - idle: calm breathing/blink micro-loop
   - running-right / running-left: locomotion facing and traveling that way,
     visibly alternating stride
   - waving: greeting with a clear raise and return
   - jumping: anticipation, lift, peak, descent, settle
   - failed: readable deflation/sadness, no floating symbols
   - waiting: expectant asking pose (needs the user), distinct from idle
   - running: focused task work / processing — NOT literal foot-running
   - review: focused inspection lean/look
   Extract each strip immediately:
   ```
   python scripts/extract_row_strip.py <strip> --expected-frames N \
     --chroma-key ff00ff --output-dir run/frames/<state> --json-out run/qa/<state>.json
   ```
   Fix or regenerate a strip whose report has errors before moving on.

3. **Look rows (9–10).** Generate two 8-pose strips: row 9 = gaze at 000
   (up), 022.5 … 157.5; row 10 = 180 … 337.5. Decide the pet's natural look
   mechanics first (eyes rotate? head turns? body bends?) and keep all 16 a
   single coherent clockwise family — never rotate the whole sprite. Extract
   with the same script into `run/frames/look` named `000.png` … `337.5.png`.

4. **Assemble + validate.**
   ```
   python scripts/compose_atlas.py --frames-root run/frames \
     --output run/spritesheet.png --webp-output run/spritesheet.webp
   python scripts/validate_atlas.py run/spritesheet.webp --json-out run/validation.json
   ```
   Validation must report `ok: true`.

5. **Visual QA.**
   ```
   python scripts/make_contact_sheet.py run/spritesheet.webp --output run/contact-sheet.png
   python scripts/render_previews.py run/spritesheet.webp --output-dir run/previews
   ```
   Read the contact sheet and preview GIFs yourself (they are images — look at
   them). Block on: identity drift between rows, wrong-facing runs, inert
   idle, clipped poses, leftover chroma fringe, look cells that don't clearly
   face their direction. Regenerate the failing row, not the whole sheet.
   Show the contact sheet to the user for approval before installing.

6. **Package.**
   ```
   ~/.claude/pets/<pet-id>/
   ├── pet.json        {"id","displayName","description","spriteVersionNumber":2,"spritesheetPath":"spritesheet.webp"}
   └── spritesheet.webp
   ```
   The overlay's pet picker (right-click) lists it immediately. The same
   package dropped into `~/.codex/pets/` works in Codex CLI.

## Repairs and imports

- Existing v2 atlas: run `validate_atlas.py`; regenerate only failing rows
  (split first with `scripts/split_atlas.py`), recompose, revalidate.
- Existing 8×9 v1 atlas (1536×1872): split it, generate only the two look
  rows, recompose as v2.
