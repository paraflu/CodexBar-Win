# CodexBar for Windows

> System tray app that shows your **real Claude Code & OpenAI Codex usage** — session limits, weekly limits, reset times, and API costs — right from the Windows taskbar.

Windows port of [steipete/CodexBar](https://github.com/steipete/CodexBar) for macOS.

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![Windows 10/11](https://img.shields.io/badge/platform-Windows%2010%2F11-lightgrey)
![License MIT](https://img.shields.io/badge/license-MIT-green)

<p align="center">
  <img src="docs/claude-v2.png" alt="CodexBar — Claude tab" width="370">
  <img src="docs/codex-v2.png" alt="CodexBar — Codex tab" width="370">
</p>

## Security note (please read)
**The v1.2.0 executable is available from the [GitHub Releases page](https://github.com/paraflu/CodexBar-Win/releases/tag/v1.2.0).** This build does not access browser data: it uses Claude Code's local credentials, Codex's `~/.codex/auth.json`, and pi agent session logs. Earlier releases v1.0.0–v1.1.2 included browser-cookie access and have been withdrawn; do not use those old executables.

The v1.2.0 exe is an unsigned, single-file PyInstaller bundle. Antivirus software may flag it heuristically. If you prefer, build it yourself from source with `build.ps1`.

## Download or build

- **Download:** [CodexBar.exe v1.2.0](https://github.com/paraflu/CodexBar-Win/releases/download/v1.2.0/CodexBar.exe). Run it directly; no installation required. Pass `--codex` if launching from a terminal to open on the Codex tab.
- **Run from source:** install [uv](https://docs.astral.sh/uv/), then:

```powershell
git clone https://github.com/paraflu/CodexBar-Win.git
cd CodexBar-Win
uv sync
uv run codexbar.py          # Claude tab first
uv run codexbar.py --codex  # Codex tab first
uv run codexbar.py --no-window-alerts  # no 5h window alerts
```

Double-click `CodexBar.bat` to sync the environment and launch from source.

### Build the exe yourself

```powershell
.\build.ps1
```

This runs `uv sync --group dev` and PyInstaller, creating `dist\CodexBar.exe` (onefile, no console).

## Requirements

- **Windows 10 or 11**
- **uv** ([install](https://docs.astral.sh/uv/)) for running/building from source; the released exe needs no Python or uv
- **Codex CLI logged in** for live Codex quota; optional for running the Claude tab
- **Claude Code CLI** logged in for live Claude quota
- **pi agent** session logs provide measured Codex-tab costs (Windows logs; WSL sessions aren't included)

## Features

- **Dual provider** — Claude Code + OpenAI Codex tabs with smooth animated transitions
- **Live usage bars** — how much of the session/weekly limit is still left, color-coded, or `—` when the quota isn't readable
- **Tray tooltip** — hovering the tray icon shows what is left on the 5h and weekly window
- **Reset countdowns** — know exactly when your limits refresh
- **Threshold alerts** — tray notification when a 5h or weekly window crosses 80% or 95% used (once per crossing; a reset re-arms it)
- **5h window alerts** — tray notification 15 min before a 5h window ends and when the next one starts, for Claude and Codex (on by default; `--no-window-alerts` turns them off). Claude needs reset times from the OAuth source, so these alerts don't fire when the CLI source supplies the data
- **Cost tracking** — Claude from `~/.claude` logs, Codex tab from pi agent's own priced logs (`~/.pi/agent/sessions`): real tokens, not an estimate
- **Native popup** — customtkinter window with rounded corners, slide-up animation
- **Provider branding** — Claude orange palette + OpenAI dark/green theme, tray icon switches per tab
- **Smart default tab** — opens on Codex (with the OpenAI tray icon) when Claude Code is not installed
- **Auto-refresh** — updates every 5 minutes
- **No extra API keys** — uses the credentials already stored by Claude Code, Codex CLI and pi agent
- **Startup option** — toggle “Start with Windows” from the tray menu (per-user, no admin required)
- **Diagnostic log** — the windowed exe writes to `%USERPROFILE%\.codexbar\codexbar.log`

## How It Gets Data

For **Claude Code**, CodexBar tries these sources in order for quota; JSONL logs also provide cost data:

| Priority | Method | What it does |
|----------|--------|-------------|
| 1 | **CLI (PTY)** | Spawns interactive `claude` via pseudo-terminal, sends `/usage`, parses output |
| 2 | **OAuth token** | Reads `~/.claude/.credentials.json`, calls Claude.ai API |
| 3 | **JSONL logs** | Scans `~/.claude/projects/` for conversation usage and cost |

For **Codex** the quota comes from the same endpoint the ChatGPT dashboard uses
(`chatgpt.com/backend-api/wham/usage`, token from `~/.codex/auth.json`), because the
quota is per account and local rollouts go stale: newer Codex builds write
`.jsonl.zst` and pi sessions write no rollouts at all. Local rollouts remain the
fallback for the quota only.

The cost in the Codex tab comes from **pi agent's session logs** in
`~/.pi/agent/sessions`: pi stores the priced usage of every assistant message
(`message.usage.cost.total`), so today's and the last-30-days figures are measured
token spend, not an estimate. Runs of pi inside WSL are not read — only the Windows
logs.

## macOS vs Windows

| | [CodexBar](https://github.com/steipete/CodexBar) (macOS) | CodexBar-Win (Windows) |
|---|---|---|
| **UI** | SwiftUI menu bar popup | customtkinter system tray popup |
| **Language** | Swift | Python |
| **Providers** | 16+ (Claude, Cursor, Copilot, Gemini...) | Claude Code + OpenAI Codex |
| **Data source** | CLI PTY | CLI PTY → OAuth → JSONL |
| **Install** | `brew install --cask steipete/tap/codexbar` | Download `CodexBar.exe` from [v1.2.0](https://github.com/paraflu/CodexBar-Win/releases/tag/v1.2.0), or build with uv |
| **Auto-update** | Sparkle framework | Manual download / rebuild |
| **Browser access** | Keychain cookie read | None |
| **Distribution** | ~5 MB (native) | Unsigned PyInstaller onefile exe; AV may flag heuristically |
| **License** | MIT | MIT |

## Troubleshooting

| Problem | Fix |
|---------|-----|
| No usage data shown | Make sure `claude` is in PATH and you're logged in |
| “winpty not found” | Run `uv sync`; confirm the app is launched with `uv run codexbar.py` |
| Codex quota unavailable | Sign in to Codex CLI so `~/.codex/auth.json` exists; refresh falls back to the last readable local rollout |
| Codex “Today” cost is `—` or $0.00 | pi must have recorded assistant usage today in `~/.pi/agent/sessions`; Windows logs only (WSL sessions are not included) |
| Popup doesn't appear | Check the system tray overflow area (click ^ arrow near clock) |
| Startup option doesn't stick | It writes a per-user Windows Run entry; no admin required, but the executable must stay at the same path |

## Credits

Windows port inspired by [steipete/CodexBar](https://github.com/steipete/CodexBar) (MIT) by [Peter Steinberger](https://steipete.me) — the original macOS menu bar app.

## License

MIT
