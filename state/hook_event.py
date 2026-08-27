#!/usr/bin/env python3
"""Vivarium hook event writer.

Wired to Claude Code's Notification / Stop / UserPromptSubmit / SessionStart
hooks. Records the latest event per session so the desktop overlay knows when
a session needs input, finished a turn, or just started — and brings the pet
up on SessionStart if it is not already running.

Must never block or fail a hook: always exits 0, prints nothing.
"""
import json
import os
import subprocess
import sys
import time

STATE_DIR = os.path.join(os.path.expanduser("~"), ".claude", "pets", ".state")
BEACON = os.path.join(STATE_DIR, "overlay.pid")


# each hook stamps its own field, so liveness never depends on one collapsed
# "last event" — a session is alive if it prompted recently and has not ended
EVENT_FIELD = {
    "SessionStart": "start_ts",
    "UserPromptSubmit": "prompt_ts",
    "Stop": "stop_ts",
    "Notification": "notify_ts",
    "SessionEnd": "ended_ts",
}


def record(data):
    """Merge this event into the session's state file."""
    sid = data.get("session_id") or "unknown"
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, sid + ".json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        state = {}
    now = time.time()
    evt = data.get("hook_event_name") or ""
    state["session_id"] = sid
    state["event"] = evt              # kept for back-compat
    state["event_ts"] = now
    field = EVENT_FIELD.get(evt)
    if field:
        state[field] = now
    if evt != "SessionEnd":
        state.pop("ended_ts", None)   # resumed or still going
    if data.get("cwd"):
        state["cwd"] = data["cwd"]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def ensure_overlay():
    """Bring the pet up if it is not already running.

    Liveness is a stale-file check on the overlay's beacon (rewritten by
    overlay/main.js every 30s) — no process APIs, no ports. Electron's
    single-instance lock is the backstop if two hooks race.
    """
    try:
        if time.time() - os.path.getmtime(BEACON) < 90:
            return          # already up and heartbeating
    except OSError:
        pass                # missing beacon -> not running

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    overlay = os.path.join(repo, "overlay")
    is_win = sys.platform == "win32"
    exe = os.path.join(overlay, "node_modules", ".bin",
                       "electron.cmd" if is_win else "electron")
    if not os.path.exists(exe):
        return              # deps not installed yet; /vivarium:setup handles it

    kwargs = {"cwd": overlay, "stdout": subprocess.DEVNULL,
              "stderr": subprocess.DEVNULL, "stdin": subprocess.DEVNULL}
    if is_win:
        # DETACHED_PROCESS | CREATE_NO_WINDOW — no console flash, outlives us
        kwargs["creationflags"] = 0x00000008 | 0x08000000
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([exe, overlay], **kwargs)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    try:
        record(data)
    except Exception:
        pass
    try:
        if data.get("hook_event_name") == "SessionStart":
            ensure_overlay()
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
