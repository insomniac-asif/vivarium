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

## Hatching a personal pet (the full flow)

When the user asks for a pet "based on what you know about me" — the highest
form of this skill — do not start from a generic mascot:

1. **Ground the identity in your memory of the user.** Their colors, their
   symbols, their story, what they are building, what they return to. The pet
   should be decodable only by them — a private mythology, not a logo. Write
   the identity as a short brief (creature, surface, accent, the one metaphor
   it embodies) and offer 2-3 name options grounded in the same material.
   Let them pick; record their choice as canon and never soften it later.
2. **Design before generating.** If Claude Design is available (the `design`
   skill), build a design canvas of the pet — a hero rendering, the nine
   state poses, look-direction head studies — so the user can reshape it by
   hand before any atlas work. The canvas is the identity contract; the
   user's edited version wins over your draft. (See `design/huma/` in this
   repo for the reference canvas of Vivarium's own pet.)
3. **Generate frames** from the approved identity — via a reference-capable
   image model (strip prompts + extract_row_strip.py), a local SDXL-class
   model (harvest-and-curate below), or by rasterizing the approved vector
   rig directly.
4. **Assemble and verify** with the deterministic scripts, review the contact
   sheet WITH the user, then package.

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

## Rig from one render (fastest path, perfect identity)

The best path when you have ONE good render of the creature and no
reference-capable image model: `rig_from_render.py` cuts the creature out and
animates it with per-frame transforms — squash and stretch, lean, bob, lift,
collapse, plus ember/glow grading — producing all 73 cells in seconds.

```
python scripts/rig_from_render.py canon.png --output-dir run/frames-rig
python scripts/compose_atlas.py --frames-root run/frames-rig \
  --output run/spritesheet.png --webp-output run/spritesheet.webp
```

Why it wins: every frame is literally the same pixels, so identity cannot
drift; the art tier equals the render's; there is no quota, no wait, and no
per-frame QA. Tune the choreography in `recipes()` / `look_recipe()` — that
table IS the animation, and it is worth iterating on per pet (a serpent leans
differently than a chibi).

Its honest limit: whole-body motion cannot raise a wing or turn a head, so
`waving` reads as a greeting bounce and the look rows as directional lean
(legitimate body language for a chibi; the v2 deadzone keeps neutral on idle).
When you later get per-limb rows from a reference-capable model, drop those
rows in over the rigged ones — the atlas is just files.

## Harvest-and-curate (recommended for local SDXL-class models)

Prompt-only SDXL will NOT reliably produce exact N-pose strips — it draws
dense multi-row model sheets, and some generations are degenerate. The path
that actually works on a local 8GB GPU:

1. Generate 10-15 sheets with varied state prompts (the exact layout doesn't
   matter — you want a large diverse pose library). Fewer, larger poses per
   sheet ("four large poses in a row") come out cleaner than many small ones.
2. `harvest_poses.py` — slices every sheet into per-pose crops via
   background-color detection + connected components, and renders labeled
   contact grids.
3. Vision-classify every crop (quality / identity / facing / head direction /
   pose type) — read the grids yourself or fan out to vision agents.
4. Generate small targeted gap sheets for whatever the library lacks (looking
   straight up, a collapse-to-ash arc, a wave) and re-harvest.
5. Write an assignment mapping each of the 73 atlas slots to a crop id (with
   optional `"mirror": true` and per-slot `"h"` target height — source sheets
   draw the character at wildly different scales, so per-slot normalization
   is essential). Keep facing coherent within each row; mirror one run
   direction from the other.
6. `curate_atlas.py assignment.json` — background-knockout (border flood),
   despill, per-slot scale, bottom-center baseline into 192x208 cells.
7. Compose, validate, contact-sheet, previews as below. Review visually,
   swap weak crops, recut — iteration is cheap after generation.

`extract_row_strip.py` remains the right tool when a model CAN follow strip
layouts (reference-conditioned models like Gemini image or FLUX Kontext).

## Repairs and imports

- Existing v2 atlas: run `validate_atlas.py`; regenerate only failing rows
  (split first with `scripts/split_atlas.py`), recompose, revalidate.
- Existing 8×9 v1 atlas (1536×1872): split it, generate only the two look
  rows, recompose as v2.
