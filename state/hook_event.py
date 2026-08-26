#!/usr/bin/env python3
"""Juna hook event writer. Wired to Claude Code Notification / Stop /
UserPromptSubmit hooks. Records the latest event per session so the desktop
overlay knows when a session needs input, finished a turn, or re-engaged.
Must never block or fail a hook: always exits 0, prints nothing."""
import json
import os
import sys
import time

try:
    data = json.load(sys.stdin)
    sid = data.get("session_id") or "unknown"
    state_dir = os.path.join(os.path.expanduser("~"), ".claude", "pets", ".state")
    os.makedirs(state_dir, exist_ok=True)
    path = os.path.join(state_dir, sid + ".json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        state = {}
    state["session_id"] = sid
    state["event"] = data.get("hook_event_name") or ""
    state["event_ts"] = time.time()
    if data.get("cwd"):
        state["cwd"] = data["cwd"]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, path)
except Exception:
    pass
sys.exit(0)
