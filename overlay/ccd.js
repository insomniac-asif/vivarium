// Reads what the Claude Code desktop app already knows about its own sessions.
//
// Two stores, both plain files, both readable with nothing but fs:
//
//   ~/.claude/sessions/<pid>.json          one per RUNNING session: the pid that
//                                          owns it, its agent session id, its
//                                          cwd, and how it was started
//   %APPDATA%/Claude/claude-code-sessions/ one per app session: the human title
//     <account>/<org>/local_<uuid>.json    the user sees in the sidebar, keyed
//                                          to the agent id by `cliSessionId`
//
// The second one is why a pet can say "Personal pet setup" instead of "Desktop":
// sessions in the app all share one working directory, so the folder name
// identifies nothing, while the title identifies exactly the session the user
// is looking for.
const fs = require('fs');
const path = require('path');
const os = require('os');
const { spawn } = require('child_process');

const HOME = os.homedir();
const REGISTRY = path.join(HOME, '.claude', 'sessions');
// Where the desktop app keeps its sessions, per platform. Confirmed present on
// Windows and on macOS 26.5; the Linux entry is the standard Electron userData
// location and has not been checked against a real install.
const STORE_ROOTS = [
  path.join(HOME, 'AppData', 'Roaming', 'Claude', 'claude-code-sessions'),
  // the Windows app ships as an MSIX package, so the Roaming path above is a
  // redirect; this is where it really lands when the redirect is not readable
  path.join(HOME, 'AppData', 'Local', 'Packages', 'Claude_pzs8sxrjxfjjc',
            'LocalCache', 'Roaming', 'Claude', 'claude-code-sessions'),
  path.join(HOME, 'Library', 'Application Support', 'Claude', 'claude-code-sessions'),
  path.join(HOME, '.config', 'Claude', 'claude-code-sessions'),
];

function alive(pid) {
  if (!pid) return false;
  try { process.kill(pid, 0); return true; } catch (e) { return e.code === 'EPERM'; }
}

// ---- running sessions -------------------------------------------------------

// Every live session, straight from the registry the CLI maintains. Returns []
// when the registry is absent, which is the caller's signal to fall back.
function runningSessions() {
  let files;
  try { files = fs.readdirSync(REGISTRY); } catch { return []; }
  const out = [];
  for (const f of files) {
    if (!f.endsWith('.json')) continue;
    let s;
    try { s = JSON.parse(fs.readFileSync(path.join(REGISTRY, f), 'utf8')); } catch { continue; }
    const sid = s && (s.sessionId || s.session_id);
    if (!sid) continue;
    const pid = s.pid || Number(f.slice(0, -5));
    if (!alive(pid)) continue;          // the file outlives the process
    out.push({
      pid,
      sessionId: sid,
      cwd: s.cwd || null,
      entrypoint: s.entrypoint || null,
      kind: s.kind || null,
      startedAt: s.startedAt || 0,
    });
  }
  return out;
}

// ---- titles -----------------------------------------------------------------

// The store holds hundreds of ~700KB files, so nothing here ever parses one
// whole: the header we want sits in the first few hundred bytes.
const HEAD = 8192;
const buf = Buffer.alloc(HEAD);

function readHead(file) {
  const fd = fs.openSync(file, 'r');
  try {
    const n = fs.readSync(fd, buf, 0, HEAD, 0);
    return buf.slice(0, n).toString('utf8');
  } finally { fs.closeSync(fd); }
}

function field(text, key) {
  const m = text.match(new RegExp('"' + key + '"\\s*:\\s*"((?:[^"\\\\]|\\\\.)*)"'));
  if (!m) return null;
  try { return JSON.parse('"' + m[1] + '"'); } catch { return null; }
}

function num(text, key) {
  const m = text.match(new RegExp('"' + key + '"\\s*:\\s*(-?\\d+(?:\\.\\d+)?)'));
  return m ? Number(m[1]) : null;
}

function flag(text, key) {
  const m = text.match(new RegExp('"' + key + '"\\s*:\\s*(true|false)'));
  return m ? m[1] === 'true' : null;
}

function storeFiles() {
  for (const root of STORE_ROOTS) {
    const out = [];
    let accounts;
    try { accounts = fs.readdirSync(root); } catch { continue; }
    // account and org ids are part of the path and change when the user signs
    // in again, so they are walked rather than hardcoded
    for (const a of accounts) {
      const pa = path.join(root, a);
      let orgs;
      try { orgs = fs.readdirSync(pa); } catch { continue; }
      for (const o of orgs) {
        const po = path.join(pa, o);
        let files;
        try { files = fs.readdirSync(po); } catch { continue; }
        for (const f of files) {
          if (f.startsWith('local_') && f.endsWith('.json')) out.push(path.join(po, f));
        }
      }
    }
    if (out.length) return out;
  }
  return [];
}

const cache = new Map();     // agent session id -> { title, ccdSessionId, file, mtime }
let lastMiss = 0;

function parseHead(file) {
  const t = readHead(file);
  const cli = field(t, 'cliSessionId');
  if (!cli) return null;
  const entry = {
    title: field(t, 'title'),
    ccdSessionId: field(t, 'sessionId'),
    cwd: field(t, 'cwd'),
    // when the app last brought this session on screen. An edge, not a state:
    // it is stamped as a session becomes visible and never refreshed while the
    // user keeps reading, so it answers "was it shown since X", not "is it
    // showing now".
    focusedAt: num(t, 'lastFocusedAt') || 0,
    file,
    mtime: 0,
  };
  // A resumed session keeps its title under a fresh agent id. The app records
  // the ids it used to answer to, but writes them at the far end of a ~700KB
  // file rather than in this header, so they are only picked up on the rare
  // occasion they land in it. That costs nothing and is not relied on: the
  // current id is what the header carries, and that is what we look up.
  const prior = t.match(/"priorCliSessionIds"\s*:\s*\[([^\]]*)\]/);
  const also = prior ? (prior[1].match(/[0-9a-fA-F-]{36}/g) || []) : [];
  return { cli, also, entry };
}

// Title for an agent session id, or null. Cheap on the hot path: a hit costs one
// statSync, and only a genuine miss walks the store — rate-limited, because a
// session the app has not written a title for yet would otherwise re-walk it on
// every tick.
function titleFor(sessionId, now) {
  if (!sessionId) return null;
  now = now || Date.now();
  const hit = cache.get(sessionId);
  if (hit) {
    try {
      const m = fs.statSync(hit.file).mtimeMs;
      if (m !== hit.mtime) {              // the app rewrites the file as it goes
        const fresh = parseHead(hit.file);
        if (fresh) Object.assign(hit, fresh.entry);
        hit.mtime = m;
      }
      return hit;
    } catch { cache.delete(sessionId); }  // file went away; fall through to a scan
  }
  if (now - lastMiss < 20000) return null;
  lastMiss = now;
  const files = storeFiles()
    .map(f => { try { return { f, m: fs.statSync(f).mtimeMs }; } catch { return null; } })
    .filter(Boolean)
    .sort((a, b) => b.m - a.m);           // the session in use was touched last
  for (const { f, m } of files) {
    let parsed;
    try { parsed = parseHead(f); } catch { continue; }
    if (!parsed) continue;
    parsed.entry.mtime = m;
    if (!cache.has(parsed.cli)) cache.set(parsed.cli, parsed.entry);
    for (const p of parsed.also) if (!cache.has(p)) cache.set(p, parsed.entry);
    if (parsed.cli === sessionId || parsed.also.indexOf(sessionId) >= 0) {
      lastMiss = 0;                       // found it; no need to hold the door shut
      return cache.get(sessionId);
    }
  }
  return null;
}

// Which session the app is actually showing. The app stamps lastFocusedAt when a
// session becomes visible, so this is its own answer rather than our guess.
function displayedSession() {
  // Reads every header, because any session can be the one on screen and only
  // the header says when it was last put there. Measured at ~33ms for 355
  // files; narrowing it by mtime was tried and came out slower, since statting
  // them all costs more than reading heads the OS has already cached. The
  // caller holds the answer for several seconds rather than asking often.
  let best = null;
  for (const f of storeFiles()) {
    let t;
    try { t = readHead(f); } catch { continue; }
    // a number of milliseconds, not a date string
    const ts = num(t, 'lastFocusedAt');
    if (!ts) continue;
    if (!best || ts > best.ts) {
      best = { ts, ccdSessionId: field(t, 'sessionId'), title: field(t, 'title'),
               cliSessionId: field(t, 'cliSessionId') };
    }
  }
  return best;
}

// ---- activation -------------------------------------------------------------

// The app registers a claude:// handler and holds a single-instance lock, so
// handing it a URL reaches the window it already has instead of opening another
// one. Going through the shell rather than the exe directly is deliberate: the
// packaged binary is not ours to execute.
//
// Two routes, and the difference matters. `code/continue?session=` reads as the
// obvious one and only restores and focuses the window — its session-navigation
// half sits behind a feature flag that is off, so it lands you wherever the app
// already was. The route that actually changes session is the one below, which
// carries no such flag. Neither is a published API, so this asks the app to
// switch and then checks whether it did, rather than assuming.
const ID_RE = /^local_[A-Za-z0-9-]{1,64}$/;
const SWITCH = 'claude://claude.ai/epitaxy/';
const FOCUS = 'claude://code/continue?session=';

// Hand a URL to whatever the desktop registers as the claude:// handler. Every
// platform has a way to do this and no two of them are the same. Going through
// the shell's opener rather than the app binary is deliberate: the packaged
// binary is not ours to execute.
const OPENER = {
  win32:  ['rundll32.exe', ['url.dll,FileProtocolHandler']],
  darwin: ['open', []],
  linux:  ['xdg-open', []],
};

function fire(url) {
  const opener = OPENER[process.platform];
  if (!opener) return false;
  try {
    const p = spawn(opener[0], opener[1].concat([url]),
                    { detached: true, stdio: 'ignore', windowsHide: true });
    // a spawn failure arrives as an event, not a throw, and an unhandled one on
    // a child process takes the whole main process down with it
    p.on('error', () => {});
    p.unref();
    return true;
  } catch { return false; }
}

function storeFileFor(ccdSessionId) {
  const name = ccdSessionId + '.json';
  for (const f of storeFiles()) if (path.basename(f) === name) return f;
  return null;
}

// When the app last put this session on screen, in ms. It stamps this itself as
// a session becomes visible, which is what makes the check below possible.
function focusedAt(ccdSessionId) {
  const f = storeFileFor(ccdSessionId);
  if (!f) return 0;
  try { return num(readHead(f), 'lastFocusedAt') || 0; } catch { return 0; }
}

// Show a session. Only ever call this for something the user just clicked: it
// pulls the app's single view away from whatever was on it, which is welcome as
// an answer to a click and hostile as a background event.
function openSession(ccdSessionId, done) {
  if (!ID_RE.test(String(ccdSessionId || ''))) {
    fire(FOCUS + 'last');                     // no id: just bring the app forward
    if (done) done('no session id, app raised');
    return false;
  }
  // Asking for the session already on screen changes nothing the app records,
  // so establish that first — otherwise a switch that had nothing to do reads
  // as a failure.
  const shown = displayedSession();
  const alreadyThere = !!shown && shown.ccdSessionId === ccdSessionId;
  const before = focusedAt(ccdSessionId);
  fire(SWITCH + ccdSessionId);
  if (alreadyThere) {
    // nothing to switch to: saying "switched" here would have hidden the fact
    // that a tap on the pet was asking for the session already on screen
    if (done) setTimeout(() => done('already showing'), 300);
    return true;
  }
  // The app stamps the session in memory as it appears but writes the file on a
  // delay, so a single peek a moment later reads the old value and calls a
  // successful switch a failure. Poll instead, and give it a few seconds.
  let waited = 0;
  const poll = setInterval(() => {
    waited += 600;
    const after = focusedAt(ccdSessionId);
    const switched = !!after && after !== before;
    if (!switched && waited < 5000) return;
    clearInterval(poll);
    // if the route ever stops working — it is undocumented and a future build
    // could gate it the way the other one is — fall back to merely surfacing
    // the app, which is still better than a click that does nothing
    if (!switched) fire(FOCUS + ccdSessionId);
    if (done) done(switched ? 'switched' : 'refused, app raised only');
  }, 600);
  return true;
}

// Is the app's window on screen, or minimised? There is no file that says so --
// the app persists nothing when it is hidden -- and asking Windows costs a
// process and about a second, so this is never called on the tick: only once,
// when a session has just finished and the answer decides whether the user can
// be assumed to have seen it.
function windowState(done) {
  // Windows only for now: the equivalents are AppleScript on macOS and a
  // toolkit-specific query on Linux. A null answer means 'cannot tell', which
  // callers already treat as 'do not assume it was read'.
  if (process.platform !== 'win32') return done(null);
  const script =
    "Add-Type -Name W -Namespace P -MemberDefinition " +
    "'[DllImport(\"user32.dll\")]public static extern bool IsIconic(IntPtr h);'; " +
    "$p = Get-Process Claude -ErrorAction SilentlyContinue | " +
    "Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1; " +
    "if ($p) { if ([P.W]::IsIconic($p.MainWindowHandle)) { 'minimized' } else { 'visible' } } " +
    "else { 'noWindow' }";
  let out = '';
  let p;
  try {
    p = spawn('powershell', ['-NoProfile', '-Command', script],
              { windowsHide: true, stdio: ['ignore', 'pipe', 'ignore'] });
  } catch { return done(null); }
  p.on('error', () => done(null));
  p.stdout.on('data', d => { out += d; });
  p.on('close', () => done(out.trim() || null));
}

module.exports = { runningSessions, titleFor, displayedSession, openSession,
                   focusedAt, windowState, alive };
