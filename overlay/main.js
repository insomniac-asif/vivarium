// Vivarium — desktop pet overlay for Claude Code. Main process.
// Renders Codex-compatible v2 sprite pets (or the built-in procedural pet)
// in a transparent always-on-top window, driven by per-session state files
// written by the Claude Code statusline heartbeat and hooks.
const { app, BrowserWindow, Menu, ipcMain, screen } = require('electron');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const os = require('os');

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
const FROM_HOOK = process.argv.includes('--from-hook');
const bootedAt = Date.now();
let emptySince = 0;
let pinned = false;   // 'Keep still': stay put instead of patrolling
let lastSessionStartTs = 0;
let lastLookDir = -1;

if (!app.requestSingleInstanceLock()) app.exit(0);

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

// A session counts as live only if it actually took a turn (prompt or start)
// and has not ended. Subagent//-command noise never carries those stamps.
function isLive(s, now) {
  if (s.ended_ts) return false;
  if (!s.prompt_ts && !s.start_ts) return false;
  return now - lastSeen(s) < 1800;
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
      // reap: ended sessions after 10 min, anything untouched for a day
      if ((s.ended_ts && now - s.ended_ts > 600) || now - lastSeen(s) > 86400) {
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
  const notify = s.notify_ts || (s.event === 'Notification' ? s.event_ts : 0) || 0;
  const stop = s.stop_ts || 0;
  const prompt = Math.max(s.prompt_ts || 0, s.start_ts || 0);
  const active = s.active_ts || 0;
  if (notify >= stop && now - notify < 900) return 'needs_you';
  if (stop && now - stop < 90) return 'done';
  if (now - active < 12) return 'working';                     // live heartbeat
  if (prompt > stop && now - prompt < 900) return 'working';   // turn in flight
  if (now - Math.max(active, prompt, stop, notify) < 1800) return 'idle';
  return 'asleep';
}

const PRECEDENCE = ['needs_you', 'working', 'done', 'idle', 'asleep'];

function aggregate() {
  const now = Date.now() / 1000;
  const states = readSessionStates();
  let mood = 'asleep';
  let lead = null;
  let sessions = 0;
  let sessionStart = false;
  for (const s of states) {
    const m = sessionMood(s, now);
    if (PRECEDENCE.indexOf(m) < PRECEDENCE.indexOf(mood)) mood = m;
    if (isLive(s, now)) sessions++;
    if (!lead || (s.active_ts || 0) > (lead.active_ts || 0)) lead = s;
    if (s.event === 'SessionStart' && (s.event_ts || 0) > lastSessionStartTs && now - s.event_ts < 10) {
      lastSessionStartTs = s.event_ts;
      sessionStart = true;
    }
  }
  // remember the shape of the session pool for locomotion
  states.sort((a, b) => (a.session_id || '').localeCompare(b.session_id || ''));
  const live = states.filter(s => isLive(s, now));
  liveSessions = live.length;
  attentionIndex = live.findIndex(s => sessionMood(s, now) === 'needs_you');
  // clicking the pet should return you to the session it is speaking for:
  // whoever needs you, else whoever ran most recently
  const speaking = attentionIndex >= 0
    ? live[attentionIndex]
    : live.slice().sort((a, b) => lastSeen(b) - lastSeen(a))[0];
  focusPid = speaking && speaking.host_pid ? speaking.host_pid : null;

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

function sessionSummaries() {
  const now = Date.now() / 1000;
  return readSessionStates()
    .filter(s => isLive(s, now))
    .sort((a, b) => lastSeen(b) - lastSeen(a))
    .slice(0, 6)
    .map(s => ({
      folder: s.cwd ? path.basename(String(s.cwd).replace(/[\/]+$/, '')) : null,
      branch: s.cwd ? gitBranch(s.cwd) : null,
      mood: sessionMood(s, now),
      age: now - lastSeen(s),
      ctx: typeof s.ctx === 'number' ? s.ctx : null,
      model: s.model ? String(s.model).toLowerCase() : null,
      cost: typeof s.cost === 'number' ? s.cost : null,
      host_pid: s.host_pid || null,
    }));
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
    focusable: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload-panel.js'),
      contextIsolation: true, nodeIntegration: false, backgroundThrottling: false,
    },
  });
  panel.setAlwaysOnTop(true, 'screen-saver');
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
  trace(`panel show sessions=${list.length} ` +
        list.map(x => `${x.folder || '?'}:${x.mood}`).join(','));
  w.webContents.send('sessions', list);
  placePanel();
  if (!w.isVisible()) w.showInactive();
}

function hidePanelSoon() {
  clearTimeout(panelTimer);
  panelTimer = setTimeout(() => {
    if (!petOver && !panelOver && panel && !panel.isDestroyed() && panel.isVisible()) {
      panel.hide();
      trace('panel hide');
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
ipcMain.on('panel-raise', (_e, pid) => {
  if (pid) { focusPid = pid; raiseSession(); }
  if (panel && !panel.isDestroyed()) panel.hide();
});

function pushState() {
  if (!win || win.isDestroyed()) return;
  const state = aggregate();
  win.webContents.send('juna-state', state);
  pinned = !!loadConfig().pinned;   // refreshed once a second, not per frame

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
  if (liveSessions > 0) { emptySince = 0; return; }
  if (!emptySince) emptySince = Date.now();
  else if (Date.now() - emptySince > 90000) app.quit();
}

// ---- look direction (cursor following, idle only) -------------------------
function pollLook() {
  if (!win || win.isDestroyed() || dragging) return;
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
const DWELL_MIN = 6000;
const DWELL_MAX = 22000;

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
    if (petHit) { petHit = false; win.setIgnoreMouseEvents(true, { forward: true }); }
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
  win.setMenu(null);
  pinned = !!cfg.pinned;      // honour it from the first frame, not a second in
  clampToWorkArea();
  screen.on('display-removed', clampToWorkArea);
  screen.on('display-metrics-changed', clampToWorkArea);
  // start transparent to clicks; the renderer reports when the cursor is
  // actually over opaque pixels and we take the mouse back just for those
  win.setIgnoreMouseEvents(true, { forward: true });
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
    } else if (held < 450) {
      trace(`gesture=tap held=${held} raise=${focusPid}`);
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
let petHit = false;
let hoverSince = 0;
ipcMain.on('hit', (_e, hit) => {
  if (hit === petHit || !win || win.isDestroyed()) return;
  petHit = hit;
  trace(`hit=${hit}`);
  win.setIgnoreMouseEvents(!hit, { forward: true });
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
    { label: 'Start with Windows', type: 'checkbox', checked: autostartEnabled(), click: (item) => setAutostart(item.checked) },
    { label: 'Stay open after the last session', type: 'checkbox',
      checked: !!loadConfig().persist, click: (item) => saveConfig({ persist: item.checked }) },
    { label: 'Keep still', type: 'checkbox', checked: !!loadConfig().pinned,
      click: (item) => saveConfig({ pinned: item.checked }) },
    { type: 'separator' },
    { label: 'Quit', click: () => app.quit() },
  ]);
  menu.popup({ window: win });
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
  beaconWrite();
  trace(`boot pid=${process.pid} trace=on`);
  createWindow();
  // after the window exists, so the beacon can publish where the pet is
  setInterval(beaconWrite, 2000);
});
app.on('window-all-closed', () => app.quit());
app.on('before-quit', beaconClear);
process.on('exit', beaconClear);
