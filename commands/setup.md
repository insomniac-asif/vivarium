---
description: Install the Vivarium overlay (npm deps), wire the statusline pet, and optionally enable autostart
---

Set up Vivarium for this user. Work through these steps, reporting progress:

1. **Locate the plugin root.** This command file lives in `<plugin-root>/commands/`; everything below is relative to `<plugin-root>`.

2. **Overlay dependencies.** Run `npm install` inside `<plugin-root>/overlay/`. Requires Node 18+. If npm is missing, tell the user and stop.

3. **Statusline pet.** Ask the user whether they want the text pet in the Claude Code status line. If yes, set in `~/.claude/settings.json`:
   ```json
   "statusLine": { "type": "command", "command": "python3 \"<plugin-root>/statusline/pet.py\"" }
   ```
   (Escape backslashes on Windows or use forward slashes.) The statusline script doubles as the overlay's heartbeat, so recommend enabling it even if they only want the desktop pet.

4. **Hooks.** The plugin's `hooks/hooks.json` registers Notification / Stop / UserPromptSubmit / SessionStart event writers automatically when the plugin is enabled — verify with the user that the plugin is enabled and do not duplicate these into settings.

5. **Launch the overlay.** Run `<plugin-root>/overlay/node_modules/.bin/electron <plugin-root>/overlay` (use `electron.cmd` on Windows) detached/in background. The pet appears bottom-right; right-click it for the pet picker (any Codex-format v2 pet in `~/.claude/pets/` or `~/.codex/pets/` is listed automatically), position reset, autostart toggle, and quit.

6. **Autostart** (optional, ask first): on Windows the right-click menu's "Start with Windows" writes a Startup-folder script; on macOS/Linux tell the user to add the electron command to their login items / autostart mechanism manually.

7. **Verify.** Confirm `~/.claude/pets/.state/` gains a `<session-id>.json` file when a Claude Code session is active, and that the overlay reacts (working → run animation).
