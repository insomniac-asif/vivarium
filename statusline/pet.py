#!/usr/bin/env python3
"""Huma — an ash-phoenix chick that lives in the Claude Code status line.

Reads the statusline JSON from stdin, keeps a small persistent state file,
and prints one ANSI-colored line. Never crashes: any failure degrades to a
minimal line so the statusline never goes blank.
"""
import json
import os
import sys
import time

STATE_HOME = os.path.join(os.path.expanduser("~"), ".claude", "pets", ".state")
STATE_PATH = os.path.join(STATE_HOME, "lifetime.json")
CONTEXT_WINDOW = 200_000  # fallback assumption for transcript-size estimate

# ANSI palette: dark ground, one red accent
RESET = "\x1b[0m"
DIM = "\x1b[38;5;245m"      # body / labels
FAINT = "\x1b[38;5;238m"    # separators
RED = "\x1b[38;5;196m"      # signal red — gills, hot meter
DARKRED = "\x1b[38;5;124m"  # cool meter fill
WHITE = "\x1b[38;5;252m"    # face


def read_stdin_json():
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    try:
        os.makedirs(STATE_HOME, exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_PATH)
    except Exception:
        pass


def find_number(obj, keys):
    """Search dict (one level of nesting) for the first plausible numeric key."""
    if not isinstance(obj, dict):
        return None
    for k in keys:
        v = obj.get(k)
        if isinstance(v, (int, float)):
            return v
    for v in obj.values():
        if isinstance(v, dict):
            n = find_number(v, keys)
            if n is not None:
                return n
    return None


def context_pct(data):
    """Best-effort context fullness 0-99. Tries known field shapes, then
    falls back to transcript file size (bytes/4 ~ tokens vs 200k window)."""
    pct = find_number(data, ["used_percentage", "percent_used", "percentage", "context_percentage"])
    if pct is not None and 0 <= pct <= 100:
        return min(99, int(pct))
    used = find_number(data, ["used_tokens", "tokens_used", "input_tokens", "context_used"])
    total = find_number(data, ["context_window", "context_window_size", "max_tokens", "total_tokens"])
    if used is not None:
        window = total if total and total > 1000 else CONTEXT_WINDOW
        return min(99, int(100 * used / window))
    tp = data.get("transcript_path")
    if tp:
        try:
            approx_tokens = os.path.getsize(tp) / 4
            return min(99, int(100 * approx_tokens / CONTEXT_WINDOW))
        except Exception:
            pass
    return None


def git_branch(cwd):
    """Cheap branch read — no subprocess. Walk up looking for .git/HEAD."""
    try:
        d = cwd
        for _ in range(6):
            head = os.path.join(d, ".git", "HEAD")
            if os.path.isfile(head):
                with open(head, "r", encoding="utf-8", errors="ignore") as f:
                    ref = f.read().strip()
                return ref.rsplit("/", 1)[-1] if ref.startswith("ref:") else ref[:7]
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
    except Exception:
        pass
    return None


def bird_sprite(pct, hour):
    """Huma: ash-phoenix chick. Wings = ember veins (bright at night, banked
    grey by day), eyes = small but hotter than the veins, sized by burn."""
    frame = int(time.time()) % 2
    wing = "~" if frame == 0 else "≈"
    night = hour >= 22 or hour < 7
    day = 7 <= hour < 15
    wing_color = RED if night else (FAINT if day else DARKRED)
    if day:
        eye, eye_color = "-", DIM          # banked, grey daylight
    elif pct is None or pct < 25:
        eye, eye_color = "·", RED          # low burn
    elif pct < 60:
        eye, eye_color = "•", RED          # lit
    elif pct < 85:
        eye, eye_color = "●", RED          # hot
    else:
        eye, eye_color = "◉", RED          # overburn — compact soon
    face = f"{WHITE}({RESET}{eye_color}{eye}{RESET}{WHITE}v{RESET}{eye_color}{eye}{RESET}{WHITE}){RESET}"
    return f"{wing_color}{wing}{RESET}{face}{wing_color}{wing}{RESET}"


def belly_meter(pct):
    if pct is None:
        return ""
    cells = 5
    filled = min(cells, int(round(pct / 100 * cells)))
    color = RED if pct >= 85 else DARKRED
    bar = color + "▰" * filled + FAINT + "▱" * (cells - filled) + RESET
    return f"{bar} {DIM}{pct}% burned{RESET}"


def level(hours):
    if hours < 5:
        return 1
    if hours < 25:
        return 2
    if hours < 100:
        return 3
    return 4


_HB_CACHE = STATE_HOME


def write_heartbeat(sid, now, pct, model, cwd, cost, lv):
    """Per-session state file consumed by the juna-desktop overlay."""
    try:
        os.makedirs(_HB_CACHE, exist_ok=True)
        path = os.path.join(_HB_CACHE, f"{sid}.json")
        try:
            if now - os.path.getmtime(path) < 2:
                return
        except OSError:
            pass
        try:
            with open(path, "r", encoding="utf-8") as f:
                hb = json.load(f)
        except Exception:
            hb = {}
        hb.update({
            "session_id": sid,
            "active_ts": now,
            "ctx": pct,
            "model": model,
            "cwd": cwd,
            "cost": cost,
            "level": lv,
        })
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(hb, f)
        os.replace(tmp, path)
    except Exception:
        pass


def main():
    data = read_stdin_json()
    state = load_state()

    model = (data.get("model") or {}).get("display_name") or ""
    cwd = (data.get("workspace") or {}).get("current_dir") or data.get("cwd") or ""
    cost = (data.get("cost") or {}).get("total_cost_usd")
    dur_ms = (data.get("cost") or {}).get("total_duration_ms") or 0
    sid = data.get("session_id") or "unknown"
    pct = context_pct(data)
    hour = time.localtime().tm_hour

    # lifetime growth: accumulate active hours across sessions
    sessions = state.setdefault("sessions", {})
    prev_ms = sessions.get(sid, 0)
    if dur_ms > prev_ms:
        state["lifetime_ms"] = state.get("lifetime_ms", 0) + (dur_ms - prev_ms)
        sessions[sid] = dur_ms
    if len(sessions) > 200:  # keep the file small
        for k in list(sessions)[:-100]:
            del sessions[k]
    now = time.time()
    if now - state.get("saved_at", 0) > 20:
        state["saved_at"] = now
        save_state(state)
    hours = state.get("lifetime_ms", 0) / 3_600_000
    lv = level(hours)

    # heartbeat for the desktop overlay (throttled to every 2s per session)
    write_heartbeat(sid, now, pct, model, cwd, cost, lv)

    sprite = bird_sprite(pct, hour)

    sep = f" {FAINT}·{RESET} "
    parts = [f"{sprite} {DIM}huma lv{lv}{RESET}"]
    loc = os.path.basename(cwd.rstrip("/\\")) if cwd else ""
    branch = git_branch(cwd) if cwd else None
    if loc:
        parts.append(f"{DIM}{loc}{RESET}" + (f"{FAINT}:{RESET}{DIM}{branch}{RESET}" if branch else ""))
    if model:
        parts.append(f"{DIM}{model.lower()}{RESET}")
    meter = belly_meter(pct)
    if meter:
        parts.append(meter)
    if isinstance(cost, (int, float)) and cost > 0:
        parts.append(f"{DIM}${cost:.2f}{RESET}")

    sys.stdout.write(sep.join(parts))


if __name__ == "__main__":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        main()
    except Exception:
        sys.stdout.write("~(·v·)~ huma")
