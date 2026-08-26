// Vivarium — desktop pet overlay for Claude Code. Main process.
// Renders Codex-compatible v2 sprite pets (or the built-in procedural pet)
// in a transparent always-on-top window, driven by per-session state files
// written by the Claude Code statusline heartbeat and hooks.
const { app, BrowserWindow, Menu, ipcMain, screen } = require('electron');
const fs = require('fs');
const path = require('path');
const os = require('os');

const HOME = os.homedir();
const STATE_DIR = path.join(HOME, '.claude', 'pets', '.state');
const CONFIG_PATH = path.join(HOME, '.claude', 'pets', '.vivarium.json');
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
let lastSessionStartTs = 0;
let lastLookDir = -1;

if (!app.requestSingleInstanceLock()) app.quit();

// ---- config ---------------------------------------------------------------
function loadConfig() {
  try { return JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf8')); } catch { return {}; }
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
      const newest = Math.max(s.active_ts || 0, s.event_ts || 0);
      if (now - newest > 86400) { fs.unlinkSync(p); continue; }
      out.push(s);
    } catch {}
  }
  return out;
}

function sessionMood(s, now) {
  const active = now - (s.active_ts || 0);
  const evAge = now - (s.event_ts || 0);
  if (s.event === 'Notification' && evAge < 900) return 'needs_you';
  if (active < 12) return 'working';
  if (s.event === 'Stop' && evAge < 90) return 'done';
  if (active < 1800) return 'idle';
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
    if (now - (s.active_ts || 0) < 1800) sessions++;
    if (!lead || (s.active_ts || 0) > (lead.active_ts || 0)) lead = s;
    if (s.event === 'SessionStart' && (s.event_ts || 0) > lastSessionStartTs && now - s.event_ts < 10) {
      lastSessionStartTs = s.event_ts;
      sessionStart = true;
    }
  }
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

function pushState() {
  if (win && !win.isDestroyed()) win.webContents.send('juna-state', aggregate());
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
  applyPet(currentPet());
  setInterval(pushState, 1000);
  setInterval(pollLook, 180);
}

// ---- drag (main follows cursor; reports direction for run animation) ------
ipcMain.on('drag-start', () => {
  if (dragTimer || !win) return;
  dragging = true;
  const start = screen.getCursorScreenPoint();
  const [wx, wy] = win.getPosition();
  const ox = start.x - wx;
  const oy = start.y - wy;
  lastCursor = start;
  win.webContents.send('juna-state', { dragging: true });
  dragTimer = setInterval(() => {
    if (!win || win.isDestroyed()) return;
    const c = screen.getCursorScreenPoint();
    win.setPosition(c.x - ox, c.y - oy);
    const dx = c.x - lastCursor.x;
    if (Math.abs(dx) > 2) win.webContents.send('juna-state', { dragging: true, dragDir: Math.sign(dx) });
    lastCursor = c;
  }, 16);
});
ipcMain.on('drag-end', () => {
  dragging = false;
  if (dragTimer) { clearInterval(dragTimer); dragTimer = null; }
  if (win && !win.isDestroyed()) {
    const [x, y] = win.getPosition();
    saveConfig({ x, y });
    win.webContents.send('juna-state', { dragging: false });
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
    { type: 'separator' },
    { label: 'Quit', click: () => app.quit() },
  ]);
  menu.popup({ window: win });
});

app.whenReady().then(createWindow);
app.on('window-all-closed', () => app.quit());
