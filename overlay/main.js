// Vivarium — desktop pet overlay for Claude Code. Main process.
// Renders Codex-compatible v2 sprite pets (or the built-in procedural pet)
// in a transparent always-on-top window, driven by per-session state files
// written by the Claude Code statusline heartbeat and hooks.
const { app, BrowserWindow, Menu, ipcMain, screen } = require('electron');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const os = require('os');
const ccd = require('./ccd');
const turns = require('./turns');

const HOME = os.homedir();
const STATE_DIR = path.join(HOME, '.claude', 'pets', '.state');
const CONFIG_PATH = path.join(HOME, '.claude', 'pets', '.vivarium.json');
const PIDFILE = path.join(STATE_DIR, 'overlay.pid');
const PET_ROOTS = [
  { root: path.join(HOME, '.claude', 'pets'), source: 'claude' },
  { root: path.join(HOME, '.codex', 'pets'), source: 'codex' },
];
const STARTUP_VBS = path.join(
  process.env.APPDATA || path.join(HOME, 'AppData', 'Roaming'),
  'Microsoft', 'Windows', 'Start Menu', 'Programs', 'Startup', 'vivarium.vbs'
);

const WIN_W = 200;
const WIN_H = 224;
const LOOK_DEADZONE = 90; // px from window center; inside -> neutral -> idle

let win = null;
let dragTimer = null;
let dragging = false;
let lastCursor = null;
let currentMood = 'idle';
let liveSessions = 0;        // sessions active in the last 30 min
let attentionIndex = -1;     // index of the session that needs input, or -1
let focusPid = null;         // window to raise when the pet is clicked
let focusCcd = null;         // the app session that window should be showing
const FROM_HOOK = process.argv.includes('--from-hook');
const bootedAt = Date.now();
let emptySince = 0;
let pinned = false;   // 'Keep still': stay put instead of patrolling
let lastSessionStartTs = 0;
let lastLookDir = -1;
let lastTracedShape = null;
let runningCount = 0;        // interactive sessions the CLI says are running
let menuClosedAt = 0;        // a click that dismisses the menu is not a tap
let petHit = false;      // pointer is over actual pet pixels
let hoverSince = 0;      // when the current hover began, for the open delay

if (!app.requestSingleInstanceLock()) app.exit(0);

// A capture request with no pet running is an error, not a launch: nobody
// asked for a pet, and the caller is waiting for a file that would never come.
if (captureTarget(process.argv)) {
  process.stderr.write('vivarium: no pet is running to capture\n');
  app.exit(2);
}

// `electron . --capture out.png` asks the pet already running to photograph
// itself. The window draws its own pixels, so this needs no screen-recording
// permission from the OS and works over ssh on a machine whose display nobody
// can see -- which is the only way to check what a pet looks like on a machine
// you are not sitting at.
app.on('second-instance', (_e, argv) => {
  const file = captureTarget(argv);
  if (file) captureTo(file);
});

// Electron mixes its own switches into argv, so the word after --capture is not
// reliably the path. Take --capture=<path>, or the next argument that is not
// itself a switch.
function captureTarget(argv) {
  for (let i = 0; i < argv.length; i++) {
    const a = String(argv[i]);
    if (a.startsWith('--capture=')) return a.slice(10) || null;
    if (a === '--capture') {
      for (let j = i + 1; j < argv.length; j++) {
        if (!String(argv[j]).startsWith('-')) return argv[j];
      }
    }
  }
  return null;
}

function captureTo(file) {
  if (!win || win.isDestroyed()) return;
  win.webContents.capturePage().then(img => {
    try {
      fs.writeFileSync(file, img.toPNG());
      trace(`captured ${img.getSize().width}x${img.getSize().height} -> ${file}`);
    } catch (e) { trace(`capture failed: ${e.message}`); }
  }).catch(e => trace(`capture failed: ${e.message}`));
}

// ---- config ---------------------------------------------------------------
function loadConfig() {
  // strip a UTF-8 BOM — Windows tools love to add one
  try { return JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf8').replace(/^﻿/, '')); } catch { return {}; }
}
function saveConfig(patch) {
  try {
    const cfg = Object.assign(loadConfig(), patch);
    fs.mkdirSync(path.dirname(CONFIG_PATH), { recursive: true });
    fs.writeFileSync(CONFIG_PATH, JSON.stringify(cfg, null, 2));
  } catch {}
}
function defaultPosition() {
  const wa = screen.getPrimaryDisplay().workArea;
  return { x: wa.x + wa.width - WIN_W - 24, y: wa.y + wa.height - WIN_H - 8 };
}

// Click-through is what lets a wandering pet never eat a click meant for what
// is under it, and `forward` is what lets it still see the pointer to notice a
// hover. Linux has no forward: a click-through window there is simply blind,
// and toggling it left the pet dead to the mouse after its first hover. So on
// Linux the pet stays interactive over its whole rect -- it eats clicks on its
// transparent pixels, which is bad, but a pet you cannot hover, menu, or quit
// is worse.
function clickThrough(on) {
  if (!win || win.isDestroyed()) return;
  if (process.platform === 'linux') return;
  win.setIgnoreMouseEvents(on, { forward: true });
}

function clampToWorkArea() {
  if (!win || win.isDestroyed()) return;
  const b = win.getBounds();
  const wa = screen.getDisplayNearestPoint({ x: b.x + b.width / 2, y: b.y + b.height / 2 }).workArea;
  const x = Math.max(wa.x, Math.min(wa.x + wa.width - WIN_W, b.x));
  const y = Math.max(wa.y, Math.min(wa.y + wa.height - WIN_H, b.y));
  if (x !== b.x || y !== b.y) {
    win.setPosition(Math.round(x), Math.round(y));
    saveConfig({ x: Math.round(x), y: Math.round(y) });
  }
}

// ---- pet discovery --------------------------------------------------------
function discoverPets() {
  const pets = [{ id: 'huma', name: 'Huma egg (built-in)', kind: 'procedural', dir: null, source: 'builtin' }];
  for (const { root, source } of PET_ROOTS) {
    let entries;
    try { entries = fs.readdirSync(root, { withFileTypes: true }); } catch { continue; }
    for (const e of entries) {
      if (!e.isDirectory() || e.name.startsWith('.')) continue;
      const dir = path.join(root, e.name);
      try {
        const meta = JSON.parse(fs.readFileSync(path.join(dir, 'pet.json'), 'utf8'));
        if (meta.spriteVersionNumber !== 2 || !meta.spritesheetPath) continue;
        if (!fs.existsSync(path.join(dir, meta.spritesheetPath))) continue;
        pets.push({ id: `${source}:${meta.id || e.name}`, name: `${meta.displayName || e.name} (${source})`, kind: 'sprite', dir, meta, source });
      } catch {}
    }
  }
  return pets;
}

function currentPet() {
  const pets = discoverPets();
  const want = loadConfig().pet;
  return pets.find(p => p.id === want) || pets[0];
}

function sheetDataUrl(pet) {
  const p = path.join(pet.dir, pet.meta.spritesheetPath);
  const mime = p.toLowerCase().endsWith('.png') ? 'image/png' : 'image/webp';
  return `data:${mime};base64,${fs.readFileSync(p).toString('base64')}`;
}

function applyPet(pet) {
  saveConfig({ pet: pet.id });
  const page = pet.kind === 'sprite' ? 'renderer.html' : 'procedural.html';
  win.loadFile(page);
  win.webContents.once('did-finish-load', () => {
    if (pet.kind === 'sprite') win.webContents.send('pet', pet.meta, sheetDataUrl(pet));
    pushState();
  });
}

// ---- state aggregation ----------------------------------------------------
function lastSeen(s) {
  return Math.max(s.active_ts || 0, s.event_ts || 0, s.prompt_ts || 0,
                  s.stop_ts || 0, s.notify_ts || 0, s.start_ts || 0);
}

function pidAlive(pid) {
  if (!pid) return null;                 // unknown, fall back to timestamps
  try { process.kill(pid, 0); return true; } catch (e) {
    return e.code === 'EPERM';           // exists but not ours to signal
  }
}

// A session counts as live only if it took a turn, has not ended, AND the
// process running it is still there. Timestamps alone kept sessions "alive"
// for half an hour after their window was closed, which is how the tray ended
// up listing sessions that no longer existed.
function isLive(s, now) {
  if (s.ended_ts) return false;
  if (!s.prompt_ts && !s.start_ts) return false;
  const alive = pidAlive(s.owner_pid);
  if (alive === false) return false;     // its process is gone: it is gone
  const window = alive === true ? 3600 : 900;   // unverifiable: trust less
  return now - lastSeen(s) < window;
}

function readSessionStates() {
  const out = [];
  let files;
  try { files = fs.readdirSync(STATE_DIR); } catch { return out; }
  const now = Date.now() / 1000;
  for (const f of files) {
    if (!f.endsWith('.json') || f === 'lifetime.json') continue;
    const p = path.join(STATE_DIR, f);
    try {
      const s = JSON.parse(fs.readFileSync(p, 'utf8'));
      // reap: ended sessions after 10 min, anything untouched for a day, and
      // sessions whose process died without a SessionEnd hook (a closed window
      // or a killed terminal never gets to say goodbye) once they are cold
      const orphaned = pidAlive(s.owner_pid) === false && now - lastSeen(s) > 300;
      if ((s.ended_ts && now - s.ended_ts > 600) || now - lastSeen(s) > 86400 || orphaned) {
        fs.unlinkSync(p);
        continue;
      }
      out.push(s);
    } catch {}
  }
  return out;
}

function sessionMood(s, now) {
  // Derive from whichever stamps exist: the statusline heartbeat (active_ts)
  // is the freshest signal but stops during long tool runs, so turn stamps
  // carry the state the rest of the time.
  let notify = s.notify_ts || (s.event === 'Notification' ? s.event_ts : 0) || 0;
  // a login toast is not a request for attention
  if (s.notify_type && !/permission|idle|elicit|input|approv|question/i.test(String(s.notify_type))) notify = 0;
  // and a request that has been answered is over: the session wrote to its
  // transcript after asking
  if (notify && s.turn && s.turn.writtenAt && s.turn.writtenAt / 1000 > notify + 1) notify = 0;
  const stop = s.stop_ts || 0;
  const prompt = Math.max(s.prompt_ts || 0, s.start_ts || 0);
  const active = s.active_ts || 0;
  if (notify >= stop && now - notify < 900) return 'needs_you';
  // hook stamps first, with the transcript settling a Stop that never arrived
  if (s.turn) {
    if (s.turn.inFlight) return 'working';
    if (Date.now() - s.turn.finishedAt < 90000) return 'done';
    return 'idle';
  }
  if (stop && now - stop < 90) return 'done';
  if (now - active < 12) return 'working';                     // live heartbeat
  if (prompt > stop && now - prompt < 900) return 'working';   // turn in flight
  if (now - Math.max(active, prompt, stop, notify) < 1800) return 'idle';
  // a session we know is running has not gone to sleep just because it has not
  // written to us; only a stale file earns 'asleep'
  return s.silent ? 'idle' : 'asleep';
}

const PRECEDENCE = ['needs_you', 'working', 'done', 'idle', 'asleep'];

function aggregate() {
  const now = Date.now() / 1000;
  const states = readSessionStates();
  const live = livePool(states, now);
  // Everything the pet expresses -- mood, dots, the jump on a new session --
  // comes from the same pool the tray lists. Reading every file on disk here
  // had the pet animating 'working' for a session that had ended, with an
  // empty tray under it.
  let mood = live.length ? 'idle' : 'asleep';
  let lead = null;
  let sessions = live.length;
  let sessionStart = false;
  for (const s of live) {
    const m = sessionMood(s, now);
    if (PRECEDENCE.indexOf(m) < PRECEDENCE.indexOf(mood)) mood = m;
    if (!lead || (s.active_ts || 0) > (lead.active_ts || 0)) lead = s;
    if (s.event === 'SessionStart' && (s.event_ts || 0) > lastSessionStartTs && now - s.event_ts < 10) {
      lastSessionStartTs = s.event_ts;
      sessionStart = true;
    }
  }
  runningCount = ccd.runningSessions().filter(r => !r.kind || r.kind === 'interactive').length;
  // Locomotion patrols one post per session, so the pool must be ordered by
  // something stable — sorting by recency would reshuffle the posts every time
  // a session took a turn and leave the pet skating between them.
  const posts = live.slice().sort((a, b) =>
    String(a.session_id || '').localeCompare(String(b.session_id || '')));
  liveSessions = posts.length;
  attentionIndex = posts.findIndex(s => sessionMood(s, now) === 'needs_you');
  // clicking the pet should return you to the session it is speaking for:
  // whoever needs you, else whoever ran most recently
  const speaking = attentionIndex >= 0
    ? posts[attentionIndex]
    : live.slice().sort((a, b) => lastSeen(b) - lastSeen(a))[0];
  focusPid = speaking && speaking.host_pid ? speaking.host_pid : null;
  // What a tap on the pet should go to. Whoever needs you, if anyone does.
  // Otherwise nothing: with several sessions open, the freshest is almost always
  // the one already on screen, so switching to it changes nothing visible and
  // reads as a broken click -- the tray opens on the same gesture, and choosing
  // from it is the honest answer.
  const single = live.length === 1;
  const target = attentionIndex >= 0 ? posts[attentionIndex] : (single ? live[0] : null);
  const targetId = target && ccd.titleFor(target.session_id, now * 1000);
  focusCcd = (targetId && targetId.ccdSessionId) || null;

  const hour = new Date().getHours();
  currentMood = mood;
  return {
    mood,
    ctxPct: lead && typeof lead.ctx === 'number' ? lead.ctx : null,
    drowsy: hour >= 7 && hour < 15,
    sessions,
    nightGlow: hour >= 22 || hour < 7,
    event: sessionStart ? 'SessionStart' : undefined,
  };
}

function projectName(dir) {
  // a bare "Desktop" or "home" tells you nothing; prefer the repo root, and
  // fall back to the folder name only when there is no repo
  if (!dir) return null;
  try {
    let d = String(dir).replace(/[\/]+$/, '');
    for (let i = 0; i < 6 && d; i++) {
      if (fs.existsSync(path.join(d, '.git'))) return path.basename(d);
      const up = path.dirname(d);
      if (up === d) break;
      d = up;
    }
    return path.basename(String(dir).replace(/[\/]+$/, '')) || null;
  } catch { return null; }
}

function gitBranch(dir) {
  // cheap: read .git/HEAD walking up, no subprocess
  try {
    let d = dir;
    for (let i = 0; i < 6 && d; i++) {
      const head = path.join(d, '.git', 'HEAD');
      if (fs.existsSync(head)) {
        const ref = fs.readFileSync(head, 'utf8').trim();
        return ref.startsWith('ref:') ? ref.split('/').pop() : ref.slice(0, 7);
      }
      const up = path.dirname(d);
      if (up === d) break;
      d = up;
    }
  } catch {}
  return null;
}

// ---- has the user seen it? -------------------------------------------------
// A session the user has read and not given new work should stop asking for
// attention. Nothing on disk says "read": the app records when a session was
// brought on screen, never that someone looked at it, and stamps nothing at all
// while the window is hidden. So this reasons from two facts and asks Windows
// only when they are not enough.
const SEEN_GRACE = 20000;      // let a finished session show briefly either way
const seenCache = new Map();   // session id -> { finishedAt, seen, askedAt }

function idleMs() {
  try { return require('electron').powerMonitor.getSystemIdleTime() * 1000; }
  catch { return 0; }
}

function seenSince(s, id, finishedAt, displayed, nowMs) {
  if (!finishedAt) return false;
  if (id && id.focusedAt >= finishedAt) return true;   // brought up after it finished
  // A decision already reached for this same output stands. It has to be
  // checked before the on-screen test below, or a session judged seen while the
  // user was reading it would come back the moment they switched away.
  const prior = seenCache.get(s.session_id);
  if (prior && prior.finishedAt === finishedAt && prior.seen) return true;
  if (!displayed) return false;
  // It is the session on screen, but it was put there before this output
  // arrived: either the user is sitting in front of it reading, or the window
  // is minimised and nobody has seen anything. Only the window manager knows,
  // and asking costs a process and about a second — so ask once per finished
  // turn, off the tick, and show the session until the answer comes back.
  const c = prior;
  // Ask once per finished turn. Ask again only when the user has just come
  // back to the machine, which is the one thing that can change the answer.
  const justBack = c && idleMs() < 15000 && nowMs - c.askedAt > 120000;
  if (!c || c.finishedAt !== finishedAt || justBack) {
    seenCache.set(s.session_id, { finishedAt, seen: false, askedAt: nowMs });
    ccd.windowState(state => {
      const cur = seenCache.get(s.session_id);
      if (!cur || cur.finishedAt !== finishedAt) return;
      // a machine nobody has touched since the turn ended is not a reader: an
      // answer sitting on an unattended screen has not been seen
      cur.seen = state === 'visible' && idleMs() < (Date.now() - finishedAt);
      trace(`seen? ${(s.id && s.id.title) || s.session_id.slice(0, 8)} window=${state} ` +
            `idle=${Math.round(idleMs() / 1000)}s -> ${cur.seen ? 'read, dropping it' : 'not read, keeping it'}`);
    });
  }
  return false;
}

// The live pool, deduped — the single answer to "what sessions are there?".
// Both the tray and the pet's own session count read this, so the dots the pet
// carries can never disagree with the rows the tray lists.
function livePool(states, now) {
  // Preferred source: the CLI writes a file per RUNNING session naming the
  // process that owns it. That turns "which sessions exist" from a guess about
  // timestamps into a fact about the process table, and it lists a session that
  // has not written a pet state file yet.
  const running = ccd.runningSessions();
  if (running.length) {
    const byId = new Map();
    for (const s of states) if (s.session_id) byId.set(s.session_id, s);
    return running
      // a one-shot `claude -p` run is not a session anyone is sitting in front of
      .filter(r => !r.kind || r.kind === 'interactive')
      .map(r => {
        const st = byId.get(r.sessionId);
        return Object.assign({}, st || {}, {
          session_id: r.sessionId,
          owner_pid: r.pid,
          cwd: r.cwd || (st && st.cwd),
          started_at: r.startedAt,
          // running, but it has not reported anything yet -- a brand new session,
          // or one whose hooks are not installed
          silent: !st,
        });
      })
      .filter(s => !s.ended_ts)
      .map(s => decorate(s, now))
      .filter(s => pending(s, now))
      .sort((a, b) => (lastSeen(b) || (b.started_at || 0) / 1000)
                    - (lastSeen(a) || (a.started_at || 0) / 1000));
  }

  // Fallback for anywhere the registry is not written: back to timestamps.
  const live = states
    .filter(s => isLive(s, now))
    .sort((a, b) => lastSeen(b) - lastSeen(a));
  // Collapse rows that describe the same place: one window, one working
  // directory, one row. Resumes and re-runs write fresh session ids, so
  // without this the pool grows an entry per restart.
  const seen = new Set();
  const unique = [];
  for (const s of live) {
    const key = `${s.owner_pid || s.host_pid || 'x'}|${(s.cwd || '').toLowerCase()}`;
    if (seen.has(key)) continue;
    seen.add(key);
    unique.push(s);
  }
  return unique;
}

// Whether a turn is running, and when the last one ended.
//
// The Stop hook is the supported answer and is used first. It has one blind
// spot: a hook that never arrived looks exactly like a turn still running, and
// a session stuck that way would sit on the pet forever claiming to work. The
// transcript is written as the turn happens, so it settles that one case.
function turnOf(s) {
  const stop = (s.stop_ts || 0) * 1000;
  const prompt = Math.max(s.prompt_ts || 0, s.start_ts || 0) * 1000;
  const t = s.turn;                          // from the transcript, may be null
  if (!stop && !prompt) return t;            // no hook ever ran here
  const writtenAt = t ? t.writtenAt || 0 : 0;
  if (prompt > stop) {
    if (t && !t.inFlight && t.finishedAt >= prompt) return t;   // the Stop was lost
    return { inFlight: true, finishedAt: stop, writtenAt };
  }
  return { inFlight: false, finishedAt: Math.max(stop, t ? t.finishedAt : 0), writtenAt };
}

// Everything a decision needs, read once: what the app calls this session, and
// whether its last turn is still running.
function decorate(s, now) {
  s.id = ccd.titleFor(s.session_id, now * 1000);
  s.turn = turns.turnState(s.session_id, s.cwd);
  s.turn = turnOf(s);
  return s;
}

// Should the pet be carrying this session at all? It should while the session
// is working or waiting on the user, and afterwards only until the user has
// seen what it produced.
function pending(s, now) {
  const nowMs = now * 1000;
  const notify = s.notify_ts || 0;
  if (notify && now - notify < 900) return true;    // blocked on you: always
  if (!s.turn) return true;                         // nothing to read: assume it matters
  if (s.turn.inFlight) return true;                 // still working
  if (nowMs - s.turn.finishedAt < SEEN_GRACE) return true;
  const displayedId = displayedCcdId(nowMs);
  const displayed = !!(s.id && s.id.ccdSessionId && s.id.ccdSessionId === displayedId);
  return !seenSince(s, s.id, s.turn.finishedAt, displayed, nowMs);
}

// Which session the app has on screen. One scan of the store headers, held for
// ten seconds — it only changes when the user switches, and the grace period
// on a just-finished session is longer than the staleness.
let displayedCache = { at: 0, id: null };
function displayedCcdId(nowMs) {
  if (nowMs - displayedCache.at < 10000) return displayedCache.id;
  const d = ccd.displayedSession();
  displayedCache = { at: nowMs, id: d ? d.ccdSessionId : null };
  return displayedCache.id;
}

function sessionSummaries() {
  const now = Date.now() / 1000;
  return livePool(readSessionStates(), now)
    // The card refreshes every second while open. Ordering by recency moved
    // rows out from under the cursor mid-click; opening order does not move.
    .slice()
    .sort((a, b) => (a.started_at || 0) - (b.started_at || 0)
                 || String(a.session_id).localeCompare(String(b.session_id)))
    .slice(0, 6)
    .map(s => {
      // The app names its sessions, and those names are what the user thinks in.
      // The folder is a poor substitute: every session in the desktop app can
      // share one working directory, so naming rows after it labelled them all
      // "Desktop". Fall back to the folder only when there is no title.
      const id = s.id || ccd.titleFor(s.session_id, now * 1000);
      return {
        title: (id && id.title) || null,
        folder: projectName(s.cwd),
        branch: s.cwd ? gitBranch(s.cwd) : null,
        mood: sessionMood(s, now),
        age: now - (lastSeen(s) || (s.started_at || 0) / 1000 || now),
        ctx: typeof s.ctx === 'number' ? s.ctx : null,
        model: (s.model || (id && id.model)) ? String(s.model || id.model).toLowerCase() : null,
        cost: typeof s.cost === 'number' ? s.cost : null,
        host_pid: s.host_pid || null,
        ccd_id: (id && id.ccdSessionId) || null,
      };
    });
}

// ---- session tray -----------------------------------------------------------
// Codex attaches an activity list to its pet; this is that. Hovering the pet
// opens a card of live sessions — what each is doing, where, how full its
// context is — and clicking a row raises that session's window.
let panel = null;
let panelOver = false;
let petOver = false;
let panelTimer = null;

function ensurePanel() {
  if (panel && !panel.isDestroyed()) return panel;
  panel = new BrowserWindow({
    width: 320, height: 200, show: false, frame: false, transparent: true,
    resizable: false, skipTaskbar: true, hasShadow: false, alwaysOnTop: true,
    // NOT focusable:false. That sets WS_EX_NOACTIVATE, and Windows answers a
    // click on a window that cannot be activated by discarding the button
    // message entirely — the renderer never sees a mousedown, so rows looked
    // alive (they highlight on hover, because move messages are unaffected)
    // while every click on them vanished. The card still opens without stealing
    // focus: showPanel uses showInactive().
    webPreferences: {
      preload: path.join(__dirname, 'preload-panel.js'),
      contextIsolation: true, nodeIntegration: false, backgroundThrottling: false,
    },
  });
  panel.setAlwaysOnTop(true, 'screen-saver');
  if (process.platform !== 'win32') {
    try { panel.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true }); } catch {}
  }
  panel.setMenu(null);
  panel.loadFile('sessions.html');
  panel.on('closed', () => { panel = null; });
  return panel;
}

function placePanel() {
  if (!panel || panel.isDestroyed() || !win || win.isDestroyed()) return;
  const b = win.getBounds();
  const p = panel.getBounds();
  const wa = screen.getDisplayNearestPoint({ x: b.x + b.width / 2, y: b.y + b.height / 2 }).workArea;
  let x = b.x + b.width / 2 - p.width / 2;
  let y = b.y - p.height + 8;                    // above the pet by default
  if (y < wa.y) y = b.y + b.height - 8;          // no room above: sit below
  x = Math.max(wa.x, Math.min(wa.x + wa.width - p.width, x));
  y = Math.max(wa.y, Math.min(wa.y + wa.height - p.height, y));
  panel.setPosition(Math.round(x), Math.round(y));
}

function showPanel() {
  trace('panel show requested');
  const w = ensurePanel();
  const list = sessionSummaries();
  trace(`panel rows=${JSON.stringify(list.map(x => ({ t: x.title, ccd: x.ccd_id, pid: x.host_pid })))}`);
  trace(`panel show sessions=${list.length} ` +
        list.map(x => `${x.folder || '?'}:${x.mood}`).join(','));
  w.webContents.send('sessions', list);
  placePanel();
  if (!w.isVisible()) w.showInactive();
}

// Put the hover machinery back to a known state. Both ends latch on 'over the
// pet' and only report changes, so if the pointer slips away unseen -- onto the
// card, or off the window while the card was on top -- nothing ever reports
// 'not over' again and the card can never reopen.
function resetHover() {
  petHit = false;
  petOver = false;
  hoverSince = 0;
  if (win && !win.isDestroyed()) {
    clickThrough(true);
    try { win.webContents.send('forget-hit'); } catch {}
  }
}

function hidePanelSoon() {
  clearTimeout(panelTimer);
  panelTimer = setTimeout(() => {
    if (!petOver && !panelOver && panel && !panel.isDestroyed() && panel.isVisible()) {
      panel.hide();
      trace('panel hide');
      resetHover();
    }
  }, 450);
}

ipcMain.on('panel-over', (_e, v) => { panelOver = !!v; if (!v) hidePanelSoon(); });
ipcMain.on('panel-size', (_e, h) => {
  if (!panel || panel.isDestroyed()) return;
  const height = Math.max(90, Math.min(430, Math.round(h) + 12));
  const b = panel.getBounds();
  if (Math.abs(b.height - height) > 3) {
    panel.setBounds({ x: b.x, y: b.y, width: b.width, height });
    placePanel();
  }
});
ipcMain.on('panel-raise', (_e, row) => {
  const pid = row && typeof row === 'object' ? row.pid : row;
  const id = row && typeof row === 'object' ? row.ccd : null;
  trace(`row-click pid=${pid || '-'} ccd=${id || '-'} raw=${JSON.stringify(row)}`);
  if (pid || id) { focusPid = pid || focusPid; focusCcd = id || null; raiseSession(); }
  if (panel && !panel.isDestroyed()) panel.hide();
  resetHover();     // otherwise no later hover can reopen the card
});

function pushState() {
  if (!win || win.isDestroyed()) return;
  const state = aggregate();
  win.webContents.send('juna-state', state);
  pinned = !!loadConfig().pinned;   // refreshed once a second, not per frame
  if (TRACE) {
    // report the pool whenever it changes, so what the tray would list can be
    // checked without having to hover the pet to find out
    const shape = sessionSummaries()
      .map(x => `${x.title || x.folder || '?'}:${x.mood}`).join(' | ');
    if (shape !== lastTracedShape) { lastTracedShape = shape; trace(`pool [${shape}]`); }
  }

  // A pet the hook started retires with the last session. One launched from
  // autostart or by hand is the user's and never self-exits. The 60s floor
  // covers the gap before the first session writes its state, and an
  // unreadable state dir must never be read as "no sessions".
  if (panel && !panel.isDestroyed() && panel.isVisible()) {
    panel.webContents.send('sessions', sessionSummaries());
    placePanel();
  }

  if (!FROM_HOOK || loadConfig().persist) return;
  if (Date.now() - bootedAt < 60000) return;
  let readable = true;
  try { fs.readdirSync(STATE_DIR); } catch { readable = false; }
  if (!readable) { emptySince = 0; return; }
  if (runningCount > 0 || liveSessions > 0) { emptySince = 0; return; }
  if (!emptySince) emptySince = Date.now();
  else if (Date.now() - emptySince > 90000) { trace('retire: no running sessions for 90s'); app.quit(); }
}

// ---- look direction (cursor following, idle only) -------------------------
function pollLook() {
  if (!win || win.isDestroyed() || dragging) return;
  // The pet page only learns about the pointer through move events it is
  // forwarded, and a pointer that jumps from the pet onto the card (a window
  // of its own) or straight off the edge never sends one. So the truth about
  // "is the pointer over us" comes from asking the OS, which we already do
  // here 5 times a second for the gaze.
  if (petOver || panelOver) {
    const c = screen.getCursorScreenPoint();
    const inside = (b) => c.x >= b.x && c.x < b.x + b.width && c.y >= b.y && c.y < b.y + b.height;
    const overPet = inside(win.getBounds());
    const overPanel = !!(panel && !panel.isDestroyed() && panel.isVisible() && inside(panel.getBounds()));
    if (!overPet && !overPanel) {
      if (petOver) trace('hit=false (cursor left, by poll)');
      petOver = false; panelOver = false;
      resetHover();
      hidePanelSoon();
    }
  }
  let dir = -1;
  if (currentMood === 'idle') {
    const c = screen.getCursorScreenPoint();
    const b = win.getBounds();
    const dx = c.x - (b.x + b.width / 2);
    const dy = c.y - (b.y + b.height / 2);
    if (Math.hypot(dx, dy) > LOOK_DEADZONE) {
      // 000 = up, clockwise. atan2(dx, -dy) gives exactly that.
      let deg = Math.atan2(dx, -dy) * 180 / Math.PI;
      if (deg < 0) deg += 360;
      dir = Math.round(deg / 22.5) % 16;
    }
  }
  if (dir !== lastLookDir) {
    lastLookDir = dir;
    win.webContents.send('juna-state', { lookDir: dir });
  }
}

// ---- locomotion -----------------------------------------------------------
// The pet keeps a POST per live session, spaced along the strip it lives on,
// and patrols between them: one session means it mostly stays put, several
// means it paces, and a session that needs you pins it to that session's post.
const WALK_SPEED = 54;           // px/s — a walk, not a slide
const DWELL_MIN = 25000;          // long enough to be a companion, not a fidget
const DWELL_MAX = 80000;

let motion = { targetX: null, dwellUntil: 0, walking: false, dir: 1, last: 0 };
let lastWalkSent = null;

// VIVARIUM_TRACE=1 logs locomotion decisions to
// ~/.claude/pets/.state/overlay-trace.log — for debugging movement.
const TRACE = !!process.env.VIVARIUM_TRACE ||
              fs.existsSync(path.join(STATE_DIR, 'TRACE'));
function trace(msg) {
  if (!TRACE) return;
  try {
    fs.appendFileSync(path.join(STATE_DIR, 'overlay-trace.log'),
                      `${new Date().toISOString()} ${msg}
`);
  } catch {}
}

function strip() {
  const b = win.getBounds();
  return screen.getDisplayNearestPoint({ x: b.x + b.width / 2, y: b.y + b.height / 2 }).workArea;
}

function postX(i, n) {
  const wa = strip();
  const span = wa.width - WIN_W;
  if (n <= 1) return wa.x + span * 0.82;                 // its usual corner
  return wa.x + span * ((i + 1) / (n + 1));              // evenly spaced posts
}

function chooseTarget() {
  const n = Math.max(1, liveSessions);
  if (attentionIndex >= 0) return postX(attentionIndex, n);   // go stand there
  if (n === 1) {
    const wa = strip();
    const base = postX(0, 1);
    return Math.max(wa.x, Math.min(wa.x + wa.width - WIN_W,
                                   base + (Math.random() - 0.5) * 140));
  }
  return postX(Math.floor(Math.random() * n), n);            // pace the posts
}

function setWalking(on, dir) {
  const sig = on ? `w${dir}` : 'stop';
  if (sig === lastWalkSent) return;
  lastWalkSent = sig;
  if (win && !win.isDestroyed()) {
    win.webContents.send('juna-state', { walking: on, dragDir: dir });
  }
}

function tickMotion() {
  if (!win || win.isDestroyed() || dragging || pinned) return;
  const now = Date.now();
  const dt = motion.last ? Math.min(0.08, (now - motion.last) / 1000) : 0;
  motion.last = now;

  if (currentMood === 'asleep') { setWalking(false, motion.dir); return; }

  const b = win.getBounds();
  if (motion.targetX === null) {
    if (now < motion.dwellUntil) { setWalking(false, motion.dir); return; }
    const t = chooseTarget();
    trace(`decide x=${b.x} target=${Math.round(t)} mood=${currentMood} live=${liveSessions}`);
    if (Math.abs(t - b.x) < 24) {                 // already there: dwell again
      motion.dwellUntil = now + DWELL_MIN + Math.random() * (DWELL_MAX - DWELL_MIN);
      return;
    }
    motion.targetX = Math.round(t);
    motion.dir = motion.targetX > b.x ? 1 : -1;
    if (petHit) { petHit = false; clickThrough(true); }
    trace(`walk x=${b.x} -> ${motion.targetX} sessions=${liveSessions} attention=${attentionIndex} mood=${currentMood}`);
  }

  const step = WALK_SPEED * dt * motion.dir;
  let nx = b.x + step;
  const arrived = (motion.dir > 0 && nx >= motion.targetX) ||
                  (motion.dir < 0 && nx <= motion.targetX);
  if (arrived) {
    nx = motion.targetX;
    motion.targetX = null;
    motion.dwellUntil = now + DWELL_MIN + Math.random() * (DWELL_MAX - DWELL_MIN);
    setWalking(false, motion.dir);
    trace(`arrived x=${nx}`);
  } else {
    setWalking(true, motion.dir);
  }
  const wa = strip();
  nx = Math.max(wa.x, Math.min(wa.x + wa.width - WIN_W, Math.round(nx)));
  win.setPosition(nx, b.y);
}

// ---- window ---------------------------------------------------------------
function createWindow() {
  const cfg = loadConfig();
  const pos = Number.isInteger(cfg.x) && Number.isInteger(cfg.y) ? { x: cfg.x, y: cfg.y } : defaultPosition();
  win = new BrowserWindow({
    width: WIN_W,
    height: WIN_H,
    x: pos.x,
    y: pos.y,
    transparent: true,
    show: false,
    frame: false,
    resizable: false,
    skipTaskbar: true,
    hasShadow: false,
    alwaysOnTop: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      backgroundThrottling: false,
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  win.setAlwaysOnTop(true, 'screen-saver');
  win.showInactive();   // never steal the keyboard from the app the user is typing in
  // Without this the pet lives on one desktop only: switch Space, or let an app
  // go fullscreen, and it is gone. Windows has no equivalent and needs none.
  if (process.platform !== 'win32') {
    try { win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true }); } catch {}
  }
  win.setMenu(null);
  pinned = !!cfg.pinned;      // honour it from the first frame, not a second in
  clampToWorkArea();
  screen.on('display-removed', clampToWorkArea);
  screen.on('display-metrics-changed', clampToWorkArea);
  // start transparent to clicks; the renderer reports when the cursor is
  // actually over opaque pixels and we take the mouse back just for those
  clickThrough(true);
  applyPet(currentPet());
  setInterval(pushState, 1000);
  setInterval(pollLook, 180);
  setInterval(tickMotion, 33);
}

// ---- drag (main follows cursor; reports direction for run animation) ------
let dragStartPos = null;
let dragMoved = 0;
let pressStarted = 0;

function raiseSession() {
  // Codex's pet is a launcher: clicking it returns you to what it represents.
  // Whatever happens next, the pet itself must not end up holding focus: it
  // took it with the click, and an overlay that keeps it eats the keyboard.
  if (win && !win.isDestroyed()) win.blur();
  if (!focusPid && !focusCcd) return;
  // Ask the app for this particular session. Sessions in the desktop app are
  // not separate windows, so raising a window can only ever land you in the app
  // -- getting to the right conversation has to go through the app itself.
  const want = focusCcd;   // captured: the tick may retarget focusCcd meanwhile
  if (want) {
    // The app's own handler restores and focuses its window and is the only
    // thing that can pick the session. The Win32 helper on top of it flashed
    // the taskbar and, worse, targeted a service: for desktop sessions the
    // ancestry walk had recorded svchost as the window.
    ccd.openSession(want, how => trace(`session-switch ${want} -> ${how}`));
    return;
  }
  if (!focusPid) return;
  if (process.platform === 'win32') {
    try {
      spawn('powershell', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                           path.join(__dirname, 'activate.ps1'), String(focusPid)],
            { detached: true, stdio: 'ignore', windowsHide: true }).unref();
    } catch {}
  }
  // the pet must not hold focus it just handed away
  if (win && !win.isDestroyed()) win.blur();
}

ipcMain.on('drag-start', () => {
  if (dragTimer || !win) return;
  dragging = true;
  const start = screen.getCursorScreenPoint();
  const [wx, wy] = win.getPosition();
  const ox = start.x - wx;
  const oy = start.y - wy;
  lastCursor = start;
  dragStartPos = start;
  dragMoved = 0;
  pressStarted = Date.now();
  win.webContents.send('juna-state', { dragging: true });
  dragTimer = setInterval(() => {
    if (!win || win.isDestroyed()) return;
    // safety net: never let a lost release strand the pet on the cursor
    if (Date.now() - pressStarted > 30000) { endDragNow(); return; }
    const c = screen.getCursorScreenPoint();
    win.setPosition(c.x - ox, c.y - oy);
    const dx = c.x - lastCursor.x;
    if (Math.abs(dx) > 2) win.webContents.send('juna-state', { dragging: true, dragDir: Math.sign(dx) });
    dragMoved = Math.max(dragMoved, Math.hypot(c.x - dragStartPos.x, c.y - dragStartPos.y));
    lastCursor = c;
  }, 16);
});
function endDragNow() { ipcMain.emit('drag-end'); }

ipcMain.on('drag-end', () => {
  dragging = false;
  if (dragTimer) { clearInterval(dragTimer); dragTimer = null; }
  if (win && !win.isDestroyed()) {
    clampToWorkArea();
    const [x, y] = win.getPosition();
    saveConfig({ x, y });
    motion.targetX = null;
    motion.dwellUntil = Date.now() + DWELL_MAX;
    // Three gestures, distinguished by distance then duration:
    //   moved       -> a drag (already handled while moving)
    //   quick tap   -> launcher: raise the session's window
    //   press+hold  -> petting
    const held = Date.now() - pressStarted;
    if (dragMoved >= 4) {
      trace(`gesture=drag moved=${Math.round(dragMoved)}`);
      win.webContents.send('juna-state', { dragging: false });
    } else if (held < 450 && Date.now() - menuClosedAt < 400) {
      trace('gesture=tap swallowed: it dismissed the menu');
      win.webContents.send('juna-state', { dragging: false });
    } else if (held < 450) {
      trace(`gesture=tap held=${held} raise=${focusPid} session=${focusCcd || '-'}`);
      win.webContents.send('juna-state', { dragging: false });
      showPanel();          // always visible feedback
      raiseSession();
    } else {
      trace(`gesture=hold held=${held} -> pet`);
      win.webContents.send('juna-state', { dragging: false, event: 'Petted' });
    }
  }
});

// ---- autostart ------------------------------------------------------------
function autostartEnabled() { return fs.existsSync(STARTUP_VBS); }
function setAutostart(on) {
  try {
    if (on) {
      const electronCmd = path.join(__dirname, 'node_modules', '.bin', 'electron.cmd');
      fs.writeFileSync(STARTUP_VBS,
        'CreateObject("WScript.Shell").Run """' + electronCmd + '"" ""' + __dirname + '""", 0, False\r\n');
    } else if (fs.existsSync(STARTUP_VBS)) fs.unlinkSync(STARTUP_VBS);
  } catch {}
}

// ---- menu -----------------------------------------------------------------
ipcMain.on('hit', (_e, hit) => {
  if (hit === petHit || !win || win.isDestroyed()) return;
  petHit = hit;
  trace(`hit=${hit}`);
  clickThrough(!hit);
  petOver = hit;
  if (hit) {
    // keep the hover clock running across brief misses at the silhouette edge,
    // so a wobbling cursor still opens the tray
    if (!hoverSince || Date.now() - hoverSince > 1500) hoverSince = Date.now();
    clearTimeout(panelTimer);
    const waited = Date.now() - hoverSince;
    panelTimer = setTimeout(showPanel, Math.max(0, 380 - waited));
  } else {
    hidePanelSoon();
  }
});

ipcMain.on('context-menu', () => {
  const pets = discoverPets();
  const active = loadConfig().pet || pets[0].id;
  const menu = Menu.buildFromTemplate([
    { label: 'Vivarium', enabled: false },
    {
      label: 'Pets',
      submenu: pets.map(p => ({
        label: p.name, type: 'radio', checked: p.id === active,
        click: () => applyPet(p),
      })),
    },
    { type: 'separator' },
    { label: 'Reset position', click: () => { const p = defaultPosition(); win.setPosition(p.x, p.y); saveConfig({ x: p.x, y: p.y }); } },
    ...(process.platform === 'win32' ? [
      { label: 'Start with Windows', type: 'checkbox', checked: autostartEnabled(), click: (item) => setAutostart(item.checked) },
    ] : []),
    { label: 'Stay open after the last session', type: 'checkbox',
      checked: !!loadConfig().persist, click: (item) => saveConfig({ persist: item.checked }) },
    { label: 'Keep still', type: 'checkbox', checked: !!loadConfig().pinned,
      click: (item) => saveConfig({ pinned: item.checked }) },
    { type: 'separator' },
    { label: 'Quit', click: () => {
        // Quit means quit: the SessionStart hook will not bring it back until
        // the user starts it by hand again (which turns spawning back on).
        saveConfig({ spawn: false });
        trace('quit: by menu; spawn-with-Claude off until launched by hand');
        app.quit();
      } },
  ]);
  menu.popup({ window: win, callback: () => { menuClosedAt = Date.now(); } });
});

// liveness beacon: SessionStart hooks spawn the overlay only when this file
// is missing or stale, so a running overlay is never duplicated
function beaconWrite() {
  try {
    fs.mkdirSync(STATE_DIR, { recursive: true });
    let rect = null;
    if (win && !win.isDestroyed()) {
      const b = win.getBounds();
      rect = { x: b.x, y: b.y, w: b.width, h: b.height };
    }
    // JSON, not a bare pid: the hook only checks this file's freshness, while
    // tooling (and the user) can now also see where the pet actually is.
    fs.writeFileSync(PIDFILE, JSON.stringify({ pid: process.pid, rect }));
  } catch {}
}
function beaconClear() {
  try { fs.unlinkSync(PIDFILE); } catch {}
}

app.whenReady().then(() => {
  // An overlay is not an application the user switches to: no Dock icon, no
  // menu bar. setMenu(null) covers Windows and Linux but does nothing on macOS.
  if (process.platform === 'darwin') {
    try { app.dock.hide(); } catch {}
    try { Menu.setApplicationMenu(null); } catch {}
  }
  beaconWrite();
  trace(`boot pid=${process.pid} trace=on fromHook=${FROM_HOOK} argv=${JSON.stringify(process.argv.slice(1))}`);
  if (!FROM_HOOK) saveConfig({ spawn: true });   // launched by hand: spawning with Claude is wanted again
  createWindow();
  // after the window exists, so the beacon can publish where the pet is
  setInterval(beaconWrite, 2000);
});
app.on('window-all-closed', () => app.quit());
app.on('before-quit', beaconClear);
process.on('exit', beaconClear);
