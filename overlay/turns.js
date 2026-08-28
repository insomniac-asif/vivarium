// Whether a session is mid-turn, and when it last finished one.
//
// The obvious source would be the Stop hook, but it does not fire in every
// Claude Code surface -- on this machine state files carry prompt stamps from
// minutes ago beside stop stamps from hours ago -- so a pet that trusted it
// would think a session had been working since breakfast.
//
// The transcript is written as the turn happens and is always there. The last
// assistant record on the main chain carries a stop_reason: a finished turn
// says end_turn (or stop_sequence / max_tokens / refusal), while a turn still
// running says tool_use or nothing at all. Only the tail is ever read.
const fs = require('fs');
const path = require('path');
const os = require('os');

const PROJECTS = path.join(os.homedir(), '.claude', 'projects');
const DONE = new Set(['end_turn', 'stop_sequence', 'max_tokens', 'refusal']);
const TAIL = 64 * 1024;

const cache = new Map();   // session id -> { size, mtime, state }

// Claude Code names a project directory after the working directory with every
// character that is not a letter or digit replaced by a dash.
function transcriptPath(sessionId, cwd) {
  if (!sessionId || !cwd) return null;
  return path.join(PROJECTS, String(cwd).replace(/[^A-Za-z0-9]/g, '-'), sessionId + '.jsonl');
}

function readTail(file, size) {
  const len = Math.min(size, TAIL);
  const buf = Buffer.alloc(len);
  const fd = fs.openSync(file, 'r');
  try { fs.readSync(fd, buf, 0, len, size - len); } finally { fs.closeSync(fd); }
  return buf.toString('utf8');
}

function scan(text) {
  const lines = text.split('\n');
  for (let i = lines.length - 1; i >= 0; i--) {
    const line = lines[i];
    if (!line || line[0] !== '{') continue;          // also drops the partial first line
    // a subagent's transcript is interleaved here; it is not the session's turn
    if (line.indexOf('"isSidechain":true') >= 0) continue;
    if (line.indexOf('"type":"assistant"') < 0) {
      // an interrupted turn ends without an assistant record
      if (line.indexOf('[Request interrupted by user]') < 0) continue;
      try {
        const o = JSON.parse(line);
        return { finishedAt: Date.parse(o.timestamp) || 0, inFlight: false };
      } catch { continue; }
    }
    let o;
    try { o = JSON.parse(line); } catch { continue; }
    if (o.type !== 'assistant' || !o.message) continue;
    return {
      finishedAt: Date.parse(o.timestamp) || 0,
      inFlight: !DONE.has(o.message.stop_reason),    // null or tool_use: still going
    };
  }
  return null;
}

// { finishedAt, inFlight } or null when it cannot be told. Costs one stat while
// nothing changes, and one 64KB read when it does.
function turnState(sessionId, cwd) {
  const file = transcriptPath(sessionId, cwd);
  if (!file) return null;
  let st;
  try { st = fs.statSync(file); } catch { return null; }
  const hit = cache.get(sessionId);
  if (hit && hit.size === st.size && hit.mtime === st.mtimeMs) return hit.state;
  let state = null;
  try { state = scan(readTail(file, st.size)); } catch {}
  cache.set(sessionId, { size: st.size, mtime: st.mtimeMs, state });
  return state;
}

module.exports = { turnState, transcriptPath };
