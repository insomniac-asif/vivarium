---
description: Install the Vivarium overlay (npm deps), wire the statusline pet, and optionally enable autostart
---

Set up Vivarium for this user. Work through these steps, reporting progress:

1. **Locate the plugin root.** This command file lives in `<plugin-root>/commands/`; everything below is relative to `<plugin-root>`.

2. **Find a Python 3 that exists here.** Do not assume `python3`: a stock
   python.org install on Windows provides `python` and `py` but no `python3`,
   while macOS provides `python3` and no `python`. Try each of these in order
   and keep the first whose version starts with `Python 3`:
   `python3 --version`, `python --version`, `py -3 --version`.
   - If one works, note it as `<py>`.
   - On Windows, be careful: `python3` may exist as a Microsoft Store stub that
     prints "Python was not found" — that is a failure, not a success, so check
     the output says `Python 3.x`, not just that the command ran.
   - If none works, tell the user Python 3 is required, point them at
     python.org (Windows) or `brew install python` (macOS), and stop. Nothing
     else in this plugin works without it.

3. **Overlay dependencies.** Run `npm install` inside `<plugin-root>/overlay/`. Requires Node 18+. If npm is missing, tell the user and stop.
   - Note for plugin installs: `<plugin-root>` is a versioned directory that
     changes when the plugin updates, and `node_modules` does not come along.
     After an update, run this step again. The pet rewrites its own autostart
     entry each time it starts, so that heals itself.

4. **Statusline pet.** Ask the user whether they want the text pet in the Claude Code status line. If yes, set in `~/.claude/settings.json`, using the `<py>` you found:
   ```json
   "statusLine": { "type": "command", "command": "<py> \"<plugin-root>/statusline/pet.py\"" }
   ```
   (Use forward slashes, or escape backslashes.) The statusline script doubles as the overlay's heartbeat, so recommend enabling it even if they only want the desktop pet.

5. **Hooks.** The plugin's `hooks/hooks.json` registers Notification / Stop / UserPromptSubmit / SessionStart / SessionEnd event writers automatically when the plugin is enabled (each tries `python3` and falls back to `python`, so it works wherever step 2 found an interpreter) — verify with the user that the plugin is enabled and do not duplicate these into settings.

6. **Launch the overlay.** Run `<plugin-root>/overlay/node_modules/.bin/electron <plugin-root>/overlay` (use `electron.cmd` on Windows) detached/in background. The pet appears bottom-right; right-click it for the pet picker (any Codex-format v2 pet in `~/.claude/pets/` or `~/.codex/pets/` is listed automatically), position reset, autostart toggle, and quit.

7. **Autostart** (optional, ask first): on Windows the right-click menu's "Start with Windows" writes a Startup-folder script; on macOS/Linux tell the user to add the electron command to their login items / autostart mechanism manually.

8. **Verify.** Confirm `~/.claude/pets/.state/` gains a `<session-id>.json` file when a Claude Code session is active, and that the overlay reacts (working → run animation).
