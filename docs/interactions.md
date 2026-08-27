# Pet interactions

What the ecosystem does, and what Vivarium implements.

## Verified behavior elsewhere

- **Codex pet (official)**: clicking the pet focuses/opens the app — a
  launcher, not a toy; an attached tray opens activities. Draggable overlay.
  No petting, feeding, or sounds. State triggers: running = agent working,
  waiting = needs approval/input, review = completed with unread activity,
  failed = error; priority when several chats compete:
  needs-input > blocked > ready > running. The 16 look-direction rows are
  specified to follow the pointer with a neutral deadzone, but reportedly are
  never sampled in current builds.
- **Claude Code `/buddy`** (leaked easter egg): `pet` command played a ~2.5s
  floating-hearts animation with a unique in-character response each time;
  speech bubbles reacted to session activity.
- **Third-party renderers**: drag plays running-left/right by direction
  (zzp1221), right-click stats + token-fed XP/evolution (AgentPet),
  hook-driven state on every tool call (petdex).

## What Vivarium implements

- **Cursor-look, actually working**: when idle, the pet's gaze follows the
  pointer through all 16 look cells (22.5° steps, 000 = up), with the
  contract's neutral deadzone falling back to the idle loop.
- **Petting**: a press that does not move (<4px) is a pet, not a drag —
  sprite pets play the waving row; the built-in egg hops and flares its
  embers for ~1.2s.
- **Drag**: directional — the pet plays running-left/right by drag direction
  while following the cursor.
- **Session tray** (Codex attaches an activity list to its pet; this is that):
  hover the pet for ~0.4s and a card opens listing every live session — the
  folder and git branch, what it is doing, how long since it moved, the model,
  the cost, and a context-pressure meter that turns pale past 85%. Clicking a
  row raises that session's window. The card follows the pet, refreshes every
  second while open, and hides when you move away.
- **Click = launcher** (Codex's actual behaviour): a quick tap opens the tray
  AND raises the window of the session the pet is speaking for — whoever needs
  input, else whoever ran most recently. Each session records its host window
  by walking its own process ancestry.
- **Press and hold** (>450ms) pets instead of launching, so the two gestures
  never collide.
- **Spawns with Claude**: a SessionStart hook starts the overlay if it is not
  already running (liveness via a beacon file the overlay refreshes, so it is
  never duplicated). A pet the hook started retires ~90s after the last
  session ends; one you launched yourself, or via autostart, stays until you
  quit it. "Stay open after the last session" pins it either way.
- **Click-through**: the window is transparent to the mouse except over
  actual pet pixels, so a wandering pet never eats a click meant for what is
  underneath it.
- **State mapping** (aggregated across all sessions):
  needs_you (waiting) > working (running) > done (wave one-shot) > idle >
  asleep; SessionStart plays a jump. `failed` and `review` render but have no
  session signal yet — when a failure feed exists it slots above `done` in
  the priority queue, mirroring Codex's needs-input > blocked > ready order.

## Not implemented (deliberately, for now)

- Per-session pets. One pet aggregates every session and counts them with
  dots; N transparent always-on-top windows costs far more than it adds.
- Feeding/XP/evolution retention mechanics.
- Sounds and speech bubbles (voice work is parked).
- Vertical movement: the v2 atlas has no vertical locomotion rows.
