# Codex-bar

A Herdr bottom bar for folder/Git status, the Codex model and context of the focused Codex pane, and your Claude Code 5-hour and weekly quota.

## Install

Copy `mods/codex-bar/` to `%USERPROFILE%\.config\herdr-app\mods\codex-bar\`, then add this block to a layout:

```json
{
  "id": "codex-bar",
  "type": "block",
  "content": { "mod": "codex-bar" },
  "size": { "px": 28 }
}
```

The Claude quota is read on Windows with `python` (Claude Code must be logged in). The Codex context is read on the Herdr machine `wsl` with `python3` (optional). See `README.md` for data handling, requirements and troubleshooting.
