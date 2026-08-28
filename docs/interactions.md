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

- **Cursor-look, actually working**: when idle, a sprite pet's gaze follows the
  pointer through all 16 look cells (22.5° steps, 000 = up), with the
  contract's neutral deadzone falling back to the idle loop. The built-in egg
  has no look frames and ignores this.
- **Petting**: press and hold — over 450ms without moving 4px — sprite pets
  play the waving row; the built-in egg hops and flares its embers for ~1.2s.
  A shorter press is a tap, which is the launcher below.
- **Drag**: directional — a sprite pet plays running-left/right by drag
  direction while following the cursor; the egg simply follows.
- **Session tray** (Codex attaches an activity list to its pet; this is that):
  hover the pet for ~0.4s and a card opens listing every live session — the
  project and git branch, what it is doing, how long since it moved, the model,
  the cost, and a context-pressure meter that turns pale past 85%. Clicking a
  row raises that session's window. The card follows the pet, refreshes every
  second while open, and hides when you move away.
- **Rows are named after the session, not the folder**: the app names its
  sessions, and those names are what you think in. The folder is a poor
  substitute — every session in the desktop app can sit in the same directory,
  which labelled them all alike. The name comes from the app's own session
  store, matched to the running session on its id (including the ids a resumed
  session used to answer to), and falls back to the repository or folder name
  when there is no title. The folder still shows on the second line.
- **Liveness is the process, not a timer**: each session records the pid of the
  process running it, so one whose window was closed or killed disappears at
  once instead of lingering for the timeout — a window that dies has no chance
  to fire SessionEnd. Sessions the pid cannot be read for fall back to a short
  time window. One window and one working directory is one row, so a resumed
  session does not show up twice, and the row is named for the repository
  rather than whatever folder happens to be underneath it. The dots the pet
  carries and the rows the tray lists come from the same pool, so they cannot
  disagree.
- **Click = launcher** (Codex's actual behaviour): a quick tap opens the tray
  and goes to the session that needs you. If none does it opens the tray and
  leaves the choice to you rather than switching — with several sessions open
  the freshest is almost always the one already on screen, so going to it would
  be a click that visibly does nothing. Clicking a tray row goes to that row's
  session; rows are listed in the order the sessions were opened, so they do
  not reshuffle under the cursor while the card refreshes.
  In the desktop app that means the session, not just the window: sessions
  there are tabs in one window, so raising a window can only ever land you in
  the app. The pet asks the app for the session by name through its own
  `claude://` handler, then checks whether the app actually moved — the app
  records which session it is showing — and falls back to simply surfacing the
  window if it did not. Terminal sessions still raise their window by the
  process ancestry each session records.
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
