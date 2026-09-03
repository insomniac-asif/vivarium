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
    sid = data.get("session_id")
    if not sid:
        return                        # nothing to attribute this to; do not invent a session
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
    if evt == "Notification":
        state["notify_type"] = (data.get("notification_type") or data.get("type")
                                or data.get("title") or "")
    if evt != "SessionEnd":
        state.pop("ended_ts", None)   # resumed or still going
    if data.get("cwd"):
        state["cwd"] = data["cwd"]
    # Record the window this session lives in. On SessionStart always, and on
    # a later event if it is still missing, so a session that predates this
    # feature (or whose ancestry walk failed once) heals itself instead of
    # staying unclickable forever.
    if evt == "SessionStart" or not state.get("host_pid"):
        host = find_host_window_pid()
        if host:
            state["host_pid"] = host
    if evt == "SessionStart" or not state.get("owner_pid"):
        owner = find_owner_pid()
        if owner:
            state["owner_pid"] = owner
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, path)


_PROC_CACHE = None


def _proc_parents():
    """pid -> (ppid, name) for every process. Windows via toolhelp, POSIX via
    /proc. Returns {} if unavailable — callers must tolerate that.

    Snapshotting several hundred processes is the most expensive thing this
    script does, and one hook asks for it twice (the owning process and the
    window to raise). The answer cannot meaningfully change inside a single
    run, so take it once.
    """
    global _PROC_CACHE
    if _PROC_CACHE is not None:
        return _PROC_CACHE
    out = {}
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class ENTRY(ctypes.Structure):
                _fields_ = [("dwSize", wintypes.DWORD),
                            ("cntUsage", wintypes.DWORD),
                            ("th32ProcessID", wintypes.DWORD),
                            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                            ("th32ModuleID", wintypes.DWORD),
                            ("cntThreads", wintypes.DWORD),
                            ("th32ParentProcessID", wintypes.DWORD),
                            ("pcPriClassBase", ctypes.c_long),
                            ("dwFlags", wintypes.DWORD),
                            ("szExeFile", ctypes.c_char * 260)]

            k32 = ctypes.windll.kernel32
            snap = k32.CreateToolhelp32Snapshot(0x2, 0)      # TH32CS_SNAPPROCESS
            if snap == -1:
                return out
            e = ENTRY(); e.dwSize = ctypes.sizeof(ENTRY)
            ok = k32.Process32First(snap, ctypes.byref(e))
            while ok:
                out[int(e.th32ProcessID)] = (
                    int(e.th32ParentProcessID),
                    e.szExeFile.decode("utf-8", "replace").lower())
                ok = k32.Process32Next(snap, ctypes.byref(e))
            k32.CloseHandle(snap)
        except Exception:
            pass
    else:
        try:
            for name in os.listdir("/proc"):
                if not name.isdigit():
                    continue
                try:
                    with open(f"/proc/{name}/stat", encoding="utf-8") as f:
                        parts = f.read().rsplit(") ", 1)[1].split()
                    with open(f"/proc/{name}/comm", encoding="utf-8") as f:
                        comm = f.read().strip().lower()
                    out[int(name)] = (int(parts[1]), comm)
                except Exception:
                    continue
        except Exception:
            pass
        if not out:
            # macOS has no /proc. ps is in the base system on every unix and
            # answers the same question; comm may be a full path, so take the
            # last component.
            try:
                ps = subprocess.run(["ps", "-Ao", "pid=,ppid=,comm="],
                                    capture_output=True, text=True, timeout=5)
                for line in ps.stdout.splitlines():
                    parts = line.split(None, 2)
                    if len(parts) < 3:
                        continue
                    try:
                        out[int(parts[0])] = (int(parts[1]),
                                              parts[2].rsplit("/", 1)[-1].strip().lower())
                    except ValueError:
                        continue
            except Exception:
                pass
    _PROC_CACHE = out
    return out


# processes that merely relay the hook, never the window the user looks at
_RELAY = ("python", "python3", "py", "node", "bun", "bash", "sh", "zsh",
          "conhost", "cmd")


def find_owner_pid():
    """The process actually running this session (the Claude CLI/app process).

    Liveness by timestamp alone is a guess: a session whose window was closed
    without a SessionEnd hook keeps looking alive for half an hour. If the
    process that owns it is gone, the session is gone.
    """
    table = _proc_parents()
    if not table:
        return None
    pid = os.getpid()
    for _ in range(14):
        entry = table.get(pid)
        if not entry:
            return None
        ppid, _n = entry
        parent = table.get(ppid)
        if not parent:
            return None
        stem = parent[1].rsplit(".", 1)[0]
        if stem in ("claude", "node", "bun"):
            return ppid
        pid = ppid
    return None


def pid_alive(pid):
    """Is this process still around?

    Not os.kill(pid, 0): on Windows that raises the same OSError (winerror 87)
    for a process that is running as for one that never existed, so it cannot
    tell them apart -- measured. The process table can, it is already built for
    the ancestry walk, and it costs about 20ms for five hundred processes.
    """
    table = _proc_parents()
    if not table:
        return True                     # cannot tell: do not act on a guess
    return pid in table


def _cmdline(pid):
    """The command line of a process, or "" if it cannot be read."""
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_Process -Filter 'ProcessId=%d').CommandLine" % pid],
                capture_output=True, text=True, timeout=6)
            return out.stdout or ""
        if os.path.exists("/proc/%d/cmdline" % pid):
            with open("/proc/%d/cmdline" % pid, "rb") as f:
                return f.read().replace(b"\0", b" ").decode("utf-8", "replace")
        out = subprocess.run(["ps", "-o", "args=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5)
        return out.stdout or ""
    except Exception:
        return ""


HEADLESS_FLAGS = ("-p", "--print", "--no-session-persistence")


def is_headless(pid):
    """True for a `claude -p` / `--print` run: output goes to a pipe, nobody is
    watching a window, and a pet for it would appear out of nowhere.

    Match argument tokens rather than searching the raw string: an install path
    with a space in it can contain something that reads exactly like the flag,
    and a user whose path happened to look that way would never get a pet at
    all. shlex keeps a quoted path in one piece; the first token is the
    executable and is skipped either way.
    """
    args = _cmdline(pid)
    if not args:
        return False
    try:
        import shlex
        tokens = shlex.split(args, posix=False)[1:]
    except Exception:
        tokens = args.split()[1:]
    for t in tokens:
        t = t.strip('"')
        if t in HEADLESS_FLAGS or t.split("=", 1)[0] in HEADLESS_FLAGS:
            return True
    return False


def find_host_window_pid():
    """Walk up from this hook to the first ancestor that plausibly owns a
    visible window — the terminal or IDE the session is running in. That is
    what clicking the pet should bring to the front."""
    table = _proc_parents()
    if not table:
        return None
    # Collect every app-like ancestor, then take the OUTERMOST one: an app can
    # spawn helper processes of its own name (a desktop app's UI process is the
    # outer one), and only the outer process owns the visible window.
    candidates = []
    pid = os.getpid()
    for _ in range(14):
        entry = table.get(pid)
        if not entry:
            break
        ppid, _name = entry
        parent = table.get(ppid)
        if not parent:
            break
        stem = parent[1].rsplit(".", 1)[0]
        # stop at the session/init boundary. launchd is macOS pid 1, and
        # walking past it returned launchd itself as "the window to raise".
        if "explorer" in stem or stem in ("services", "wininit", "systemd",
                                          "init", "launchd", "loginwindow",
                                          "sshd", "login", "svchost",
                                          "runtimebroker", "sihost"):
            break
        if stem not in _RELAY:
            candidates.append(ppid)
        pid = ppid
    # pid 1 is never a window anyone can raise
    while candidates and candidates[-1] <= 1:
        candidates.pop()
    return candidates[-1] if candidates else None


def electron_binary(overlay):
    """The Electron executable itself, not the npm shim.

    The shim in node_modules/.bin is a batch file on Windows that runs node,
    which runs Electron -- so launching it leaves a cmd and a node process
    parented over the pet for its whole life, and because the hook spawns
    detached, that console app allocates a console window of its own. A visible
    "node" terminal, for a pet that is supposed to be an invisible overlay.
    Electron's own binary is a GUI executable with no console at all.

    The package records where it put the binary in path.txt; the platform
    defaults are the fallback.
    """
    pkg = os.path.join(overlay, "node_modules", "electron")
    try:
        with open(os.path.join(pkg, "path.txt"), encoding="utf-8") as f:
            rel = f.read().strip()
        if rel:
            exe = os.path.join(pkg, "dist", rel)
            if os.path.exists(exe):
                return exe
    except Exception:
        pass
    for rel in ("electron.exe", "electron",
                os.path.join("Electron.app", "Contents", "MacOS", "Electron")):
        exe = os.path.join(pkg, "dist", rel)
        if os.path.exists(exe):
            return exe
    return None


def ensure_overlay():
    """Bring the pet up if it is not already running.

    Liveness is a stale-file check on the overlay's beacon (rewritten by
    overlay/main.js every 30s) — no process APIs, no ports. Electron's
    single-instance lock is the backstop if two hooks race.
    """
    # Quit means quit. The pet clears this when the user quits it from its
    # menu, and sets it again when they launch it by hand.
    try:
        with open(os.path.join(os.path.dirname(STATE_DIR), ".vivarium.json"),
                  encoding="utf-8") as f:
            if json.load(f).get("spawn") is False:
                return
    except Exception:
        pass
    try:
        if time.time() - os.path.getmtime(BEACON) < 90:
            # fresh beacon, but is the process behind it alive? A crashed or
            # killed pet leaves one that reads as alive for 90 seconds, during
            # which no session start would bring the pet back.
            alive = True
            try:
                with open(BEACON, encoding="utf-8") as f:
                    pid = int(json.load(f).get("pid") or 0)
                if pid > 0:
                    alive = pid_alive(pid)
            except Exception:
                pass                    # cannot tell: assume it is up
            if alive:
                return          # already up and heartbeating
    except OSError:
        pass                # missing beacon -> not running

    # Only now, with a spawn actually on the table, is it worth asking the
    # expensive question. A one-shot `claude -p` run -- a scheduled job, an
    # orchestrator's subprocess -- is not a session anyone is sitting in front
    # of. The CLI's registry does not say so (measured: a -p run registers as
    # kind=interactive, entrypoint=claude-desktop), but the process's own
    # command line does, and reading it costs a process and about a second --
    # which is why this sits after the beacon check rather than before it. A
    # scheduled job every two minutes would otherwise pay that 720 times a day
    # to be told a pet was already running.
    try:
        owner = find_owner_pid()
        if owner and is_headless(owner):
            return
    except Exception:
        pass
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    overlay = os.path.join(repo, "overlay")
    is_win = sys.platform == "win32"
    exe = electron_binary(overlay)
    if not exe:
        return              # deps not installed yet; /vivarium:setup handles it

    kwargs = {"cwd": overlay, "stdout": subprocess.DEVNULL,
              "stderr": subprocess.DEVNULL, "stdin": subprocess.DEVNULL}
    if is_win:
        # DETACHED_PROCESS | CREATE_NO_WINDOW — no console flash, outlives us
        kwargs["creationflags"] = 0x00000008 | 0x08000000
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([exe, overlay, "--from-hook"], **kwargs)


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
