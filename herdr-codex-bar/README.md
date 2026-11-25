# Codex-bar for Herdr

A bottom bar for [Herdr](https://herdr.dev) (`herdr-app`). It shows the focused folder and Git status, the Codex model and context of the focused Codex pane, and your Claude Code 5-hour and weekly quota.

- **Left:** folder and Git chips. Push, pull and fetch run only when you click them in the branch menu.
- **Right:** Claude 5-hour and weekly quota (Windows), and the model and context of the focused Codex pane (WSL).

## Data and privacy

- **Claude quota (Windows):** `collector.py quota` reads the OAuth token from `%USERPROFILE%\.claude\.credentials.json` and sends one authenticated GET to `https://api.anthropic.com/api/oauth/usage`. It is called at most every 5 minutes, failures included. If the call fails, the last good value is shown with its age. This endpoint is not documented by Anthropic and may change.
- **Codex context (WSL):** `collector.py context` reads recent plain-JSONL rollouts in `~/.codex/sessions` to find the single fresh session whose working directory matches the focused pane. It makes no network calls. Prompt text is never returned to the mod.
- **Cache:** `quota-cache.json` next to `collector.py` holds only the percentages, reset times and status. It never holds the token. It is created at runtime and must not be shared.
- Git status is read locally.

## Requirements

- Herdr, with its config folder at `%USERPROFILE%\.config\herdr-app`.
- **Windows:** Python 3 on `PATH` as `python`, and Claude Code logged in (for the Claude quota).
- **WSL (optional, for Codex context):** a Herdr machine named `wsl` with `python3` and the Codex CLI signed in.

## Install

1. Copy the `mods/codex-bar/` folder to `%USERPROFILE%\.config\herdr-app\mods\codex-bar\`. Do not copy `quota-cache.json` or `__pycache__`.
2. Add this block to the layout where the bar should appear (for example the bottom column of `layouts/<name>.json`). The mod's `mod.json` sets `"place": "none"`, so it shows only where a layout puts it:

   ```json
   {
     "id": "codex-bar",
     "type": "block",
     "content": { "mod": "codex-bar" },
     "size": { "px": 28 }
   }
   ```

3. Check the mod, then look for errors:

   ```powershell
   herdr-app check "$env:USERPROFILE\.config\herdr-app\mods\codex-bar"
   herdr-app status
   ```

4. Make sure the bar is visible: `layout.switch` (`ctrl-shift-l`) to the layout you edited.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Claude chip shows `—` | `python` is not on `PATH`, or Claude Code is not logged in. The popup shows the reason: `auth_unavailable`, `api_unavailable` or `http_error` (the API is rate-limited; it retries later) |
| No Codex model or context | The focused pane is not Codex, the machine is not named `wsl`, or no single fresh session matches the pane's folder |

## Test

From the CodexBar-Win repository root:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s .\herdr-codex-bar\tests -v
```

The tests use synthetic fixtures. They never contact Anthropic or ChatGPT, never read real credentials, and write their cache to a temporary folder.

## Files

- `mods/codex-bar/main.js`: the mod's view.
- `mods/codex-bar/collector.py`: Claude quota and Codex context. Standard library only.
- `tests/test_collector.py`: unit tests for the collector.
