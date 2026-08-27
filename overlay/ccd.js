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
const STORE_ROOTS = [
  path.join(HOME, 'AppData', 'Roaming', 'Claude', 'claude-code-sessions'),
  // the app ships as an MSIX package, so the Roaming path above is a redirect;
  // this is where it really lands when the redirect is not readable
  path.join(HOME, 'AppData', 'Local', 'Packages', 'Claude_pzs8sxrjxfjjc',
            'LocalCache', 'Roaming', 'Claude', 'claude-code-sessions'),
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
    file,
    mtime: 0,
  };
  // a resumed session keeps its title but gets a fresh agent id, so the ids it
  // used to answer to have to map to the same entry
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
// handing it a URL restores and focuses the window it already has instead of
// opening another one. Going through the shell rather than the exe directly is
// deliberate: the packaged binary is not ours to execute.
//
// Whether the app then switches to the session named in the URL is the app's
// call, not ours — in this build that half is behind a feature gate and does
// nothing. The focus half is reliable, so this is worth firing either way.
function openSession(ccdSessionId) {
  const target = /^local_[A-Za-z0-9-]{1,64}$/.test(String(ccdSessionId || ''))
    ? ccdSessionId : 'last';
  try {
    const p = spawn('rundll32.exe',
      ['url.dll,FileProtocolHandler', 'claude://code/continue?session=' + target],
      { detached: true, stdio: 'ignore', windowsHide: true });
    p.unref();
    return true;
  } catch { return false; }
}

module.exports = { runningSessions, titleFor, displayedSession, openSession, alive };
