# Pet format (Codex-compatible v2)

Vivarium renders the same pet package format used by OpenAI Codex CLI custom
pets, so pets are portable between the two. This document restates the format
for interoperability; the implementation here is original.

## Package

```
<pets-dir>/<pet-id>/
├── pet.json
└── spritesheet.webp   (or .png)
```

`pet.json`:

```json
{
  "id": "pet-id",
  "displayName": "Pet Name",
  "description": "One short sentence.",
  "spriteVersionNumber": 2,
  "spritesheetPath": "spritesheet.webp"
}
```

`spriteVersionNumber: 2` is required — it declares the 11-row layout below.

Vivarium scans two roots: `~/.claude/pets/` and `~/.codex/pets/`.

## Atlas

PNG or WebP, exactly **1536×2288**, RGBA, transparent background.
Grid: **8 columns × 11 rows** of **192×208** cells.

| Row | State         | Used cols | Frame durations (ms)          |
|-----|---------------|-----------|-------------------------------|
| 0   | idle          | 0–5       | 280, 110, 110, 140, 140, 320  |
| 1   | running-right | 0–7       | 120 ×7, then 220              |
| 2   | running-left  | 0–7       | 120 ×7, then 220              |
| 3   | waving        | 0–3       | 140 ×3, then 280              |
| 4   | jumping       | 0–4       | 140 ×4, then 280              |
| 5   | failed        | 0–7       | 140 ×7, then 240              |
| 6   | waiting       | 0–5       | 150 ×5, then 260              |
| 7   | running       | 0–5       | 120 ×5, then 220              |
| 8   | review        | 0–5       | 150 ×5, then 280              |
| 9   | look A        | 0–7       | 000, 022.5 … 157.5 degrees    |
| 10  | look B        | 0–7       | 180, 202.5 … 337.5 degrees    |

Cells after a standard row's last used column must be fully transparent.
Real-world pets sometimes carry extra frames beyond the nominal count (the
canonical Axi pet ships seven idle frames): the validator accepts a populated
idle cell 6 with a warning, the splitter/composer preserve such frames
losslessly, and the overlay plays them.
All 16 look cells are used. `000` = looking **up** (12 o'clock), proceeding
clockwise in 22.5° steps. Neutral/front is the pointer deadzone — the
renderer falls back to the idle loop when the cursor is near the pet.

## How Vivarium maps Claude Code state to rows

| Session state                        | Row           |
|--------------------------------------|---------------|
| any session actively working         | running       |
| a session waiting on permission/input| waiting       |
| a turn just finished                 | waving (once), then idle |
| a new session started                | jumping (once)|
| idle, cursor beyond deadzone         | look row cell facing the cursor |
| idle / everything asleep             | idle (asleep = slowed) |
| pet dragged left / right             | running-left / running-right |

`failed` and `review` are rendered by the engine and used by pets, but are not
yet fed by a session signal.
