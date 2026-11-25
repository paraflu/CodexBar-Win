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

### With an agent (recommended)

Paste this prompt into an agent running on the machine with Herdr:

````text
Install this Herdr mod for me. Do not commit or change anything in git.

Source: `https://github.com/paraflu/CodexBar-Win`, folder `herdr-codex-bar/mods/codex-bar` (branch `docs/update-readme-landing`). Clone or download it first.

1. Before copying anything, tell me the mod (a) sends a request to `api.anthropic.com` with my Claude token from `%USERPROFILE%\.claude\.credentials.json`, (b) runs `python` on Windows and `python3` on WSL, and (c) reads files outside its own folder. Wait for my OK.
2. Copy `mods/codex-bar/` to `%USERPROFILE%\.config\herdr-app\mods\codex-bar\`. Do not copy `quota-cache.json` or `__pycache__`. If that folder already exists, stop and ask me before overwriting.
3. Do not edit Herdr's own files (`frame/`, `*.d.ts`, `AGENTS.md`, `mods/` shipped by Herdr). Ask me which layout in `%USERPROFILE%\.config\herdr-app\layouts\` should show the bar. If it is a shipped one (`main`, `terminals`, `win98`), clone it first with `herdr-app do layout.clone '{"name":"<layout>","as":"<layout>-codex"}'` and edit the clone.
4. In that layout, add this block to the bottom column, next to the other blocks. Keep every existing block and make sure `id` is unique:

   ```json
   {
     "id": "codex-bar",
     "type": "block",
     "content": { "mod": "codex-bar" },
     "size": { "px": 28 }
   }
   ```

5. Run `herdr-app check "%USERPROFILE%\.config\herdr-app\mods\codex-bar"` and fix any error. Then run `herdr-app status` and report any error it shows.
6. Check the prerequisites and tell me what is missing, without changing anything: `python --version` must print Python 3; the file `%USERPROFILE%\.claude\.credentials.json` must exist (Claude Code logged in). Do not print the contents of that file.
7. Finish by telling me how to see the bar (`ctrl-shift-l` to switch to the layout) and what the Claude chip shows if it is not logged in (`—`).
````

### Manual

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
