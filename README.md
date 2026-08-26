# Vivarium

Desktop pets for Claude Code.

A transparent, always-on-top desktop pet that reacts to your live Claude Code
sessions — working, waiting on you, finishing a turn, idling, sleeping — plus a
text pet for the status line, and a **hatch-pet skill** that lets Claude
generate brand-new pets for you.

Vivarium renders the **Codex-compatible v2 sprite format** (`pet.json` + an
8×11 spritesheet atlas), so pets hatched by OpenAI Codex CLI work here as-is:
drop them in `~/.codex/pets/` or `~/.claude/pets/` and they appear in the pet
picker. See [docs/format.md](docs/format.md) for the full contract.

## What it does

- **Desktop overlay** (Electron): the pet sits on your desktop and plays the
  right animation for the aggregate state of *all* your Claude Code sessions —
  `running` while Claude works, `waiting` when a session needs your input,
  a one-shot `wave` when a turn completes, a `jump` when a session starts,
  directional runs while you drag it, and slowed idle when everything sleeps.
- **v2 look directions, actually implemented**: when idle, the pet's gaze
  follows your mouse cursor around all 16 clockwise look cells, with the
  contract's neutral deadzone falling back to idle — a spec'd Codex feature
  reportedly dormant even in Codex's own builds.
- **Petting**: click the pet (a press that doesn't move) and it reacts —
  sprite pets wave, the built-in egg hops and flares its embers. Dragging
  plays directional run animations. See [docs/interactions.md](docs/interactions.md).
- **Status line pet**: a zero-dependency text pet (`~(•v•)~`) showing context
  pressure ("% burned"), model, git branch, and session cost. It doubles as
  the overlay's heartbeat.
- **Hatch pets with Claude**: the bundled `hatch-pet` skill turns art into a
  pet. Give it **one image** — an AI render, an illustration, anything — and
  `rig_from_render.py` animates it into all 73 atlas cells in seconds with
  squash-and-stretch, lean, bob, collapse and ember grading; identity cannot
  drift because every frame is the same pixels. Or generate per-frame art with
  a connected image model and run it through the deterministic slicing,
  assembly, validation, contact-sheet and preview pipeline.
- **Multi-session aware**: tiny dots under the pet count your live sessions.

## Install

```
/plugin marketplace add insomniac-asif/vivarium
/plugin install vivarium
/vivarium:setup
```

The setup command installs the overlay's npm dependencies, offers the
statusline pet, launches the overlay, and (on Windows) can enable autostart.
Requirements: Node 18+, Python 3 with Pillow (for hatching only).

## How state flows

No servers, no ports — just files:

```
statusline pet.py  ──(heartbeat ≤2s)──▶  ~/.claude/pets/.state/<session>.json
hooks (Notification/Stop/UserPromptSubmit/SessionStart) ──▶  same files
overlay main.js  ──(reads 1s)──▶  aggregate mood ──▶  sprite row
```

## Built-in pet

Ships with **Huma**, a procedurally-animated ash-phoenix chick — soot-black
coal body, signal-red ember veins that ignite while Claude works, eyes hotter
than the veins, banked grey in daylight, overburning past 85% context. No
spritesheet required, so the overlay works before you hatch or install any
sprite pet. (Named for the Persian-Islamic bird that never completes its
burning cycle.)

## Credits

The pet package format is interoperable with OpenAI Codex CLI custom pets;
the implementation here is original. MIT licensed.
