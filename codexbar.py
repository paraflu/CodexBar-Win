"""
CodexBar for Windows v1.2.0
============================
System tray app that shows your REAL Claude usage.
Native customtkinter popup. Uses only the local Claude Code CLI and its
credentials file — it never touches your browser.

Requirements: uv sync
Usage: uv run codexbar.py [--codex] [--no-window-alerts]     (build.ps1 produces dist/CodexBar.exe)
"""

import os
import sys
import json
import time
import re
import math
import tempfile
import shutil
import threading
import ctypes
import base64
import webbrowser
from pathlib import Path
from datetime import datetime, timedelta
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError


def _reset_timestamp(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError, OverflowError):
            return None


def _window_seconds(blob):
    if not isinstance(blob, dict):
        return None
    for key, multiplier in (("limit_window_seconds", 1), ("window_seconds", 1), ("window_minutes", 60)):
        try:
            value = float(blob[key]) * multiplier
            if math.isfinite(value) and value > 0:
                return value
        except (KeyError, TypeError, ValueError):
            pass
    return None



def _quota_forecast(used_pct, reset_at, observed_at, window_seconds, now=None):
    """Project average usage to reset; return None when timing is unreliable."""
    try:
        used_pct = float(used_pct)
        reset_at = float(reset_at)
        observed_at = float(observed_at)
        window_seconds = float(window_seconds)
        now = time.time() if now is None else float(now)
    except (TypeError, ValueError):
        return None
    if not all(map(math.isfinite, (used_pct, reset_at, observed_at, now, window_seconds))):
        return None
    if not 0 <= used_pct <= 100 or reset_at <= now or observed_at > now or now - observed_at > 600:
        return None
    remaining = reset_at - now
    elapsed = window_seconds - (reset_at - observed_at)
    if elapsed < max(600, window_seconds * 0.05) or remaining >= window_seconds:
        return None
    projected = used_pct * window_seconds / elapsed
    if not math.isfinite(projected):
        return None
    return projected, elapsed, remaining



def _quota_guidance(used, projected):
    if used >= 100 or projected > 100:
        return "Rallenta: al ritmo attuale supererai il limite"
    if projected >= 80:
        return "In linea: mantieni il ritmo"
    return "Puoi accelerare: la proiezione resta sotto l'80%"



def _quota_forecast_display(used, reset_at, observed_at, duration, text_color, now=None):
    result = _quota_forecast(used, reset_at, observed_at, duration, now)
    if result is None:
        return "Previsione non disponibile: durata o reset non affidabili", text_color
    projected = result[0]
    warning = projected > 100
    return (f"Proiezione: {projected:.0f}% al reset · {_quota_guidance(used, projected)}",
            "#F0A43B" if warning else text_color)



def _reset_warning_due(used_pct, reset_at, now):
    """True in the last minute before a window resets, when it is above 90% used."""
    return reset_at is not None and used_pct > 90 and 0 < reset_at - now <= 60


def _window_event(reset_at, now, lead=900, grace=300):
    """'ending' in the last `lead` s before a 5h reset, 'started' within `grace` s after it."""
    if reset_at is None:
        return None
    left = reset_at - now
    if 0 < left <= lead:
        return "ending"
    if -grace < left <= 0:
        return "started"
    return None


def _record_quota_sample(history, data, now=None):
    now = time.time() if now is None else now
    if data.get("source") != "api":
        return False
    recorded = False
    for name, prefix in (("session", "session"), ("weekly", "weekly")):
        try:
            used = float(data[f"{prefix}_used_pct"])
            reset_at = float(data[f"{prefix}_reset_at"])
            observed_at = float(data[f"{prefix}_observed_at"])
            duration = float(data[f"{prefix}_window_seconds"])
        except (KeyError, TypeError, ValueError):
            continue
        if (not all(map(math.isfinite, (used, reset_at, observed_at, duration)))
                or not 0 <= used <= 100 or duration <= 0 or observed_at > now
                or now - observed_at > 180 or reset_at <= now
                or reset_at - observed_at > duration):
            continue
        samples = history[name]
        if samples and (abs(samples[-1]["reset_at"] - reset_at) > 120
                        or samples[-1]["duration"] != duration):
            samples.clear()
        if samples and observed_at <= samples[-1]["at"]:
            continue
        samples.append({"at": observed_at, "used": used,
                        "reset_at": reset_at, "duration": duration})
        del samples[:-10080]
        recorded = True
    return recorded


def _quota_change_text(samples, now=None, poll="~1 min"):
    if len(samples) < 2:
        return f"Raccolta da avvio · quota account aggregata · poll {poll}"
    now = time.time() if now is None else now
    previous, latest = samples[-2:]
    minutes = max(1, round((latest["at"] - previous["at"]) / 60))
    age_minutes = max(0, round((now - latest["at"]) / 60))
    age = "ora" if age_minutes <= 1 else f"{age_minutes} min fa"
    delta = latest["used"] - previous["used"]
    return f"Variazione account: {delta:+.0f} punti in {minutes} min · ultimo campione {age} · dati da avvio · poll {poll}"


def _check_quota_forecast():
    now = 1_700_000_000
    assert math.isclose(_quota_forecast(20, now + 4 * 3600, now, 5 * 3600, now)[0], 100)
    assert math.isclose(_quota_forecast(10, now + 2 * 86400, now, 7 * 86400, now)[0], 14)
    assert _reset_warning_due(91, now + 30, now)
    assert not _reset_warning_due(90, now + 30, now)
    assert not _reset_warning_due(95, now + 120, now)
    assert not _reset_warning_due(95, now - 1, now)
    assert not _reset_warning_due(95, None, now)
    assert _window_event(now + 900, now) == "ending" and _window_event(now + 901, now) is None
    assert _window_event(now, now) == "started" and _window_event(now - 299, now) == "started"
    assert _window_event(now - 301, now) is None and _window_event(None, now) is None
    assert _quota_forecast(0, now + 4 * 3600, now, 5 * 3600, now)[0] == 0
    assert math.isclose(_quota_forecast(90, now + 4 * 3600, now, 5 * 3600, now)[0], 450)
    assert math.isclose(_quota_forecast(30, now + 1800, now, 3600, now)[0], 60)
    assert math.isclose(_window_seconds({"limit_window_seconds": 3600}), 3600)
    assert math.isclose(_window_seconds({"window_minutes": 180}), 10800)
    assert _window_seconds({}) is None
    assert _quota_forecast(20, None, now, 5 * 3600, now) is None
    assert _quota_forecast(20, now, now, 5 * 3600, now) is None
    assert _quota_forecast(20, now + 4 * 3600 + 55 * 60, now, 5 * 3600, now) is None
    assert _quota_forecast(20, now + 3600, now - 601, 5 * 3600, now) is None
    assert math.isclose(_quota_forecast(100, now + 3600, now, 5 * 3600, now)[0], 125)
    assert _quota_guidance(100, 125).startswith("Rallenta")
    assert _quota_guidance(90, 95).startswith("In linea")
    assert _quota_guidance(20, 40).startswith("Puoi accelerare")
    overrun, overrun_color = _quota_forecast_display(25, now + 4 * 3600, now, 5 * 3600, "#ccc", now)
    assert "125%" in overrun and overrun_color == "#F0A43B"
    assert _quota_forecast(10, now - 1, now, 5 * 3600, now) is None
    assert _quota_forecast(101, now + 3600, now, 5 * 3600, now) is None
    history = {"session": [], "weekly": []}
    sample = {"source": "api", "session_used_pct": 10, "weekly_used_pct": 40,
              "session_reset_at": now + 4 * 3600, "weekly_reset_at": now + 2 * 86400,
              "session_observed_at": now, "weekly_observed_at": now,
              "session_window_seconds": 5 * 3600, "weekly_window_seconds": 7 * 86400}
    assert _record_quota_sample(history, sample, now)
    sample = dict(sample, session_used_pct=15, weekly_used_pct=42,
                  session_observed_at=now + 60, weekly_observed_at=now + 60)
    assert _record_quota_sample(history, sample, now + 60)
    assert "+5 punti" in _quota_change_text(history["session"])
    assert "+2 punti" in _quota_change_text(history["weekly"])
    assert not _record_quota_sample(history, dict(sample, source="sessions"), now + 60)
    assert not _record_quota_sample(history, dict(sample, session_observed_at=now - 181), now + 60)
    sample = dict(sample, session_used_pct=16, session_observed_at=now + 180)
    assert _record_quota_sample(history, sample, now + 180)
    assert "in 2 min" in _quota_change_text(history["session"], now + 180)
    sample = dict(sample, session_reset_at=now + 4 * 3600 + 600,
                  session_observed_at=now + 240)
    assert _record_quota_sample(history, sample, now + 240)
    assert len(history["session"]) == 1
    with tempfile.TemporaryDirectory() as directory:
        event_at = "2023-11-14T22:13:20Z"
        event = {"timestamp": event_at, "payload": {"type": "token_count", "rate_limits": {
            "primary": {"used_percent": 25, "resets_at": now + 3600, "window_minutes": 300}}}}
        path = Path(directory) / "session.jsonl"
        path.write_text(json.dumps(event), encoding="utf-8")
        limits, observed_at = CodexDataFetcher._extract_rate_limits(path)
        assert limits["primary"]["window_minutes"] == 300
        assert observed_at == _reset_timestamp(event_at)
        assert math.isclose(_quota_forecast(25, now + 3600, observed_at,
                                            _window_seconds(limits["primary"]), now)[0], 31.25)
        event.pop("timestamp")
        path.write_text(json.dumps(event), encoding="utf-8")
        _, observed_at = CodexDataFetcher._extract_rate_limits(path)
        assert observed_at is None
        assert _quota_forecast(25, now + 3600, observed_at, 18000, now) is None



if getattr(sys, 'frozen', False):
    # The PyInstaller windowed exe has no console: keep the diagnostics on disk.
    # shortcut: append-only, never rotated; upgrade if the file ever matters
    try:
        _log_dir = Path.home() / '.codexbar'
        _log_dir.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = open(_log_dir / 'codexbar.log', 'a',
                                       buffering=1, encoding='utf-8', errors='replace')
        print(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} ===")
    except Exception:
        pass

try:
    from PIL import Image, ImageDraw
except ImportError:
    print("ERROR: Pillow not found. Run: python -m pip install Pillow")
    sys.exit(1)

try:
    import pystray
    from pystray import MenuItem, Menu
except ImportError:
    print("ERROR: pystray not found. Run: python -m pip install pystray")
    sys.exit(1)

try:
    import customtkinter as ctk
except ImportError:
    print("ERROR: customtkinter not found. Run: python -m pip install customtkinter")
    sys.exit(1)

try:
    from winpty import PtyProcess
except ImportError:
    PtyProcess = None
    print("[CodexBar] winpty not found (pip install pywinpty). CLI /usage disabled.")


def _resource_path(relative_path):
    """Get absolute path to resource — works for dev and PyInstaller .exe."""
    if getattr(sys, 'frozen', False):
        base = Path(sys._MEIPASS)
    else:
        base = Path(__file__).parent
    return base / relative_path


def _cost_today_text(d):
    """'—' when no log covers today: $0.00 would claim a number nobody measured."""
    if not d.get("cost_today_known", False):
        return "—"
    return f"${d['cost_today']:.2f}"


# ─────────────────────────────────────────────
# Run at Windows startup  (HKCU\...\Run, no admin needed)
# ─────────────────────────────────────────────

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_NAME = "CodexBar"


def _startup_command():
    """The command line Windows should run at login."""
    if getattr(sys, 'frozen', False):
        exe = Path(sys.executable)
    else:
        # dev checkout: pythonw so no console window flashes at login
        exe = Path(sys.executable).with_name('pythonw.exe')
        if not exe.exists():
            exe = Path(sys.executable)
    script = "" if getattr(sys, 'frozen', False) else f' "{Path(__file__).resolve()}"'
    flag = "".join(f" {f}" for f in ("--codex", "--no-window-alerts") if f in sys.argv)
    return f'"{exe}"{script}{flag}'


def startup_enabled():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.QueryValueEx(key, _RUN_NAME)
            return True
    except (ImportError, OSError):
        return False


def set_startup(enabled):
    """Add or remove the login entry; returns True when the registry agreed."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, _RUN_NAME, 0, winreg.REG_SZ, _startup_command())
            else:
                try:
                    winreg.DeleteValue(key, _RUN_NAME)
                except FileNotFoundError:
                    pass
        print(f"[CodexBar] Start with Windows: {'on' if enabled else 'off'}")
        return True
    except (ImportError, OSError) as e:
        print(f"[CodexBar] Start with Windows failed: {e}")
        return False


# ─────────────────────────────────────────────
# Data fetcher
# ─────────────────────────────────────────────

class ClaudeDataFetcher:
    def __init__(self):
        self.data = self._empty()

    def _empty(self):
        return {
            "provider": "Claude", "plan": "Unknown", "updated": "Never",
            "session_used_pct": 0, "session_reset": "unknown", "session_reset_at": None,
            "session_observed_at": None, "weekly_used_pct": 0, "weekly_reset": "unknown",
            "weekly_reset_at": None, "weekly_observed_at": None,
            "session_window_seconds": None, "weekly_window_seconds": None,
            "opus_used_pct": 0,
            "cost_today": 0.0, "cost_today_tokens": "0", "cost_today_known": False,
            "cost_30d": 0.0, "cost_30d_tokens": "0",
            "source": "none", "error": None,
            "installed": False,
        }

    def _is_claude_installed(self):
        """Check if Claude Code is installed (CLI in PATH or ~/.claude exists)."""
        claude_dir = Path.home() / ".claude"
        if claude_dir.exists():
            return True
        if self._find_claude():
            return True
        if shutil.which("claude") or shutil.which("claude.cmd"):
            return True
        return False

    def fetch_all(self):
        print("[CodexBar] Fetching real usage data...")
        got_usage = False

        # 1) Try CLI
        cli = self._fetch_cli()
        if cli and cli.get("source") == "cli":
            self.data = cli
            got_usage = True
            print(f"  OK CLI: session {cli['session_used_pct']}%, weekly {cli['weekly_used_pct']}%")
        else:
            print("  -- CLI: not available")

        # 2) Try OAuth token from ~/.claude/.credentials.json
        if not got_usage:
            api = self._fetch_oauth_api()
            if api and api.get("source") == "api":
                self.data = api
                got_usage = True
                print(f"  OK OAuth: session {api['session_used_pct']}%, weekly {api['weekly_used_pct']}%")
            else:
                print("  -- OAuth: not available")

        # 3) Always try JSONL for cost data
        cost = self._fetch_jsonl()
        if cost:
            self.data["cost_today"] = cost["cost_today"]
            self.data["cost_today_tokens"] = cost["cost_today_tokens"]
            self.data["cost_today_known"] = cost["cost_today_known"]
            self.data["cost_30d"] = cost["cost_30d"]
            self.data["cost_30d_tokens"] = cost["cost_30d_tokens"]
            if self.data["source"] == "none":
                self.data["source"] = "logs"
            print(f"  OK Logs: today ${cost['cost_today']:.2f}, 30d ${cost['cost_30d']:.2f}")
        else:
            print("  -- Logs: no JSONL found")

        self.data["updated"] = datetime.now().strftime("Updated %H:%M")
        self.data["installed"] = self._is_claude_installed()
        return self.data

    def _fetch_cli(self):
        """Spawn an interactive Claude session via PTY, send /usage, parse."""
        if PtyProcess is None:
            return None
        cmd = self._find_claude()
        if not cmd:
            return None
        try:
            raw = self._pty_usage(cmd)
            if raw and "%" in raw and ("session" in raw.lower() or "week" in raw.lower()):
                return self._parse_usage(raw)
        except Exception as e:
            print(f"    CLI err: {e}")
        return None

    @staticmethod
    def _pty_usage(cmd, startup_wait=5, trust_wait=3, cmd_wait=8):
        """Open claude in a PTY, send /usage, collect output, send /exit."""
        # Spawn in a *fresh, empty* directory. Claude Code refuses to start
        # a new session in any folder that already has a project context
        # (file lock / sessions/ pid file in ~/.claude/projects/<hash>),
        # which used to slam shut the PTY mid-startup ("Pty is closed").
        # ~/.codexbar-pty-cwd is unique to us and never has projects in it.
        import tempfile as _tf
        neutral_cwd = Path.home() / ".codexbar-pty-cwd"
        try:
            neutral_cwd.mkdir(exist_ok=True)
        except Exception:
            neutral_cwd = Path(_tf.gettempdir())
        cmd_quoted = f'"{cmd}"' if " " in cmd else cmd
        # Strip PyInstaller-injected env vars (_MEIPASS, _PYI_*) so the
        # spawned Node-based claude.exe doesn't inherit a sanitized
        # interpreter path that aborts startup.
        clean_env = {k: v for k, v in os.environ.items()
                     if k not in ("_MEIPASS", "_MEIPASS2",
                                  "_PYI_APPLICATION_HOME_DIR")
                     and not k.startswith("_PYI_")}
        if "PATH" in clean_env:
            parts = [p for p in clean_env["PATH"].split(";")
                     if "_MEI" not in p and "PyInstaller" not in p]
            clean_env["PATH"] = ";".join(parts)
        proc = PtyProcess.spawn(
            f'cmd.exe /c {cmd_quoted}',
            dimensions=(40, 120),
            cwd=str(neutral_cwd),
            env=clean_env,
        )
        chunks = []
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                try:
                    d = proc.read(8192)
                    if d:
                        chunks.append(d)
                except EOFError:
                    break
                except Exception:
                    time.sleep(0.1)

        t = threading.Thread(target=reader, daemon=True)
        t.start()

        try:
            time.sleep(startup_wait)       # wait for welcome / trust prompt
            # Accept the workspace trust prompt if shown ("Yes, I trust…")
            proc.write("\r")
            time.sleep(trust_wait)         # wait for welcome screen after trust
            proc.write("/usage\r")         # select from autocomplete + execute
            time.sleep(cmd_wait)           # wait for usage data to render
        finally:
            stop.set()
            try:
                proc.write("/exit\r")
            except Exception:
                pass
            time.sleep(1)
            try:
                proc.close(force=True)
            except Exception:
                pass
            t.join(timeout=3)

        return "".join(chunks)

    def _find_claude(self):
        places = [
            Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd",
            Path(os.environ.get("APPDATA", "")) / "npm" / "claude",
            Path.home() / ".claude" / "local" / "claude.exe",
            Path.home() / "scoop" / "shims" / "claude.cmd",
        ]
        for p in places:
            if p.exists():
                print(f"    Found claude: {p}")
                return str(p)
        r = shutil.which("claude") or shutil.which("claude.cmd")
        if r:
            print(f"    Found claude in PATH: {r}")
        return r

    def _parse_usage(self, raw):
        """Parse the /usage output from the interactive Claude CLI.

        Block-based parser that tolerates two output styles:
          A) Claude Code <= 2.0 — words separated by real spaces, e.g.
                 Current session
                 ███                 6% used
                 Resets 4pm (Europe/Malta)
          B) Claude Code 2.1+    — PTY uses cursor-positioning so spaces
             collapse on Windows after ANSI stripping, e.g.
                 Currentsession
                                       0%used
                 Resets1:40am(Europe/Malta)
        """
        # strip VT100 / ANSI / OSC / control chars
        clean = re.sub(r'\x1b\[[0-9;?]*[A-Za-z]', '', raw)
        clean = re.sub(r'\x1b\][^\x07\x1b]*[\x07]', '', clean)
        clean = re.sub(r'\x1b[()>][0-9A-Z]', '', clean)
        clean = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', clean)

        d = self._empty()
        d["source"] = "cli"

        def grab_section(header_re):
            """Slice the clean text from `header_re` up to the next
            section header (or end), returning (pct, reset) or (None, None)."""
            stop = (r'Current\s*session|'
                    r'Current\s*week|'
                    r'What\'?s\s*contributing|'
                    r'Esc\s*to\s*cancel')
            m = re.search(
                header_re + r'(?P<body>.*?)(?=' + stop + r'|\Z)',
                clean, re.I | re.S)
            if not m:
                return None, None
            chunk = m.group('body')
            pct = None
            pm = re.search(r'(\d{1,3})\s*%\s*used', chunk, re.I)
            if pm:
                pct = max(0, min(100, int(pm.group(1))))
            reset = None
            # match "Resets <value>" — value runs until newline / paren /
            # "Esc" hint. \s* between Resets and value covers the no-space case.
            rm = re.search(
                r'[Rr]es[et]{1,3}s?\s*([^\n()]+?)(?:\s*\(|Esc|$)',
                chunk)
            if rm:
                val = rm.group(1).strip().rstrip(',. ')
                # add a space between digits and letters when collapsed
                # ("May14,3am" -> "May 14, 3am") for nicer display
                val = re.sub(r'(\d)([A-Za-z])', r'\1 \2', val)
                val = re.sub(r'([A-Za-z])(\d)', r'\1 \2', val)
                val = re.sub(r',(\S)', r', \1', val)
                if len(val) >= 2:
                    reset = val.strip()
            return pct, reset

        pct, reset = grab_section(r'Current\s*session\b')
        if pct is not None:
            d["session_used_pct"] = pct
        if reset:
            d["session_reset"] = reset

        # weekly all-models — header may say "(all models)" with or without space
        pct, reset = grab_section(
            r'Current\s*week\s*\(\s*all\s*models?\s*\)')
        if pct is None:
            # fallback: any "Current week" that isn't a model-specific bucket
            pct, reset = grab_section(
                r'Current\s*week(?!\s*\(\s*(?:Opus|Sonnet|Sonet|Haiku))')
        if pct is not None:
            d["weekly_used_pct"] = pct
        if reset:
            d["weekly_reset"] = reset

        # weekly opus — "Sonet" tolerated for ANSI-mangled "Sonnet"
        pct, _ = grab_section(
            r'Current\s*week\s*\(\s*(?:Opus|Sonnet|Sonet)[^)]*\)')
        if pct is not None:
            d["opus_used_pct"] = pct

        # ── plan from welcome screen: "Claude Max" / "ClaudeMax" ──
        # capture optional "5x" / "20x" tier suffix, e.g. "Claude Max 20x"
        m = re.search(
            r'Claude\s*(Max|Pro|Team|Enterprise|Free)\s*(\d+x)?',
            clean, re.I)
        if m:
            plan = m.group(1).title()
            if m.group(2):
                plan = f"{plan} {m.group(2).lower()}"
            d["plan"] = plan

        return d

    # ── OAuth token fetcher ───────────────────

    _CREDS_PATH = Path.home() / ".claude" / ".credentials.json"

    def _fetch_oauth_api(self):
        """Read the OAuth access token that Claude Code stores locally,
        then call the Claude.ai API to get live usage data."""
        if not self._CREDS_PATH.exists():
            return None
        try:
            with open(self._CREDS_PATH, "r", encoding="utf-8") as f:
                creds = json.load(f)
            oauth = creds.get("claudeAiOauth") or {}
            token = oauth.get("accessToken")
            if not token:
                return None

            # pre-fill plan from local credentials (no network needed)
            tier = oauth.get("subscriptionType") or oauth.get("rateLimitTier") or ""
            plan_local = tier.replace("default_claude_", "").replace("_", " ").title() or "Pro"

            print(f"    OAuth token found ({len(token)} chars), plan hint: {plan_local}")
        except Exception as e:
            print(f"    OAuth creds err: {e}")
            return None

        result = self._call_claude_api(token, plan_local)
        # Even if the API call failed, populate plan from local creds
        if result is None and plan_local:
            self.data["plan"] = plan_local
        return result

    # ── shared API call logic ──────────────────

    def _call_claude_api(self, token, plan_hint):
        """GET the OAuth usage endpoint Claude Code uses: 5h and 7d windows."""
        req = Request("https://api.anthropic.com/api/oauth/usage",
                      headers={"Authorization": f"Bearer {token}",
                               "anthropic-beta": "oauth-2025-04-20",
                               "User-Agent": "Mozilla/5.0"})
        try:
            with urlopen(req, timeout=15) as resp:
                usage = json.loads(resp.read())
        except (URLError, HTTPError, json.JSONDecodeError) as e:
            print(f"    API usage err: {e}")
            return None
        d = self._empty()
        d["source"] = "api"
        d["plan"] = plan_hint
        observed = time.time()
        for prefix, key, window in (("session", "five_hour", 5 * 3600), ("weekly", "seven_day", 7 * 86400)):
            blob = usage.get(key) or {}
            d[f"{prefix}_used_pct"] = max(0, min(100, int(blob.get("utilization") or 0)))
            stamp = _reset_timestamp(blob.get("resets_at"))
            d[f"{prefix}_reset_at"] = stamp
            d[f"{prefix}_observed_at"] = observed if stamp else None
            d[f"{prefix}_window_seconds"] = window
            if stamp:
                d[f"{prefix}_reset"] = datetime.fromtimestamp(stamp).strftime("%a %H:%M")
        opus = usage.get("seven_day_opus") or {}
        d["opus_used_pct"] = max(0, min(100, int(opus.get("utilization") or 0)))
        return d

    def _fetch_jsonl(self):
        dirs = [Path.home() / ".claude" / "projects", Path.home() / ".claude"]
        total_in = total_out = total_cache = today_in = today_out = 0
        seen = set()
        today = datetime.now().date()
        nfiles = 0

        for d in dirs:
            if not d.exists(): continue
            for f in d.rglob("*.jsonl"):
                nfiles += 1
                try:
                    with open(f, 'r', encoding='utf-8', errors='ignore') as fh:
                        for line in fh:
                            line = line.strip()
                            if not line or len(line) < 10: continue
                            try: entry = json.loads(line)
                            except Exception: continue
                            if entry.get("type") != "assistant": continue
                            usage = entry.get("message",{}).get("usage",{})
                            if not usage: continue
                            mid = entry.get("message",{}).get("id","")
                            rid = entry.get("requestId","")
                            key = f"{mid}:{rid}"
                            if key in seen: continue
                            seen.add(key)
                            inp = usage.get("input_tokens",0)
                            out = usage.get("output_tokens",0)
                            cr = usage.get("cache_read_input_tokens",0)
                            cc = usage.get("cache_creation_input_tokens",0)
                            total_in += inp; total_out += out; total_cache += cr+cc
                            ts = entry.get("timestamp","")
                            if ts:
                                try:
                                    if datetime.fromisoformat(ts.replace("Z","+00:00")).date() == today:
                                        today_in += inp; today_out += out
                                except Exception: pass
                except Exception: continue

        print(f"    Scanned {nfiles} files, {len(seen)} messages")
        if total_in + total_out == 0: return None

        c30 = (total_in*3 + total_out*15 + total_cache*1.5) / 1e6
        ct = (today_in*3 + today_out*15) / 1e6

        def fmt(n):
            if n >= 1e6: return f"{n/1e6:.0f}M"
            if n >= 1e3: return f"{n/1e3:.0f}K"
            return str(n)

        return {
            "cost_today": round(ct,2), "cost_today_tokens": fmt(today_in+today_out),
            "cost_today_known": today_in + today_out > 0,
            "cost_30d": round(c30,2), "cost_30d_tokens": fmt(total_in+total_out+total_cache),
        }


# ─────────────────────────────────────────────
# Tray icon  (unchanged)
# ─────────────────────────────────────────────

def _load_logo(name="claude-logo.png", size=28):
    """Load and resize a logo from assets/."""
    logo_path = _resource_path("assets") / name
    if not logo_path.exists():
        return None
    try:
        img = Image.open(logo_path).convert("RGBA")
        w, h = img.size
        # If aspect ratio is very wide (wordmark), crop to leftmost square
        if w / h > 1.5:
            square = min(w, h)
            img = img.crop((0, 0, square, square))
        img = img.resize((size, size), Image.LANCZOS)
        return img
    except Exception:
        return None


def _make_openai_icon(size=28):
    """Generate a simple OpenAI-style icon (green hexagonal knot)."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    import math
    cx, cy = size / 2, size / 2
    r = size * 0.42
    # draw a hexagon outline
    pts = []
    for i in range(6):
        angle = math.radians(60 * i - 30)
        pts.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
    d.polygon(pts, outline=(16, 163, 127, 255), fill=None)
    # draw inner lines connecting alternating vertices
    lw = max(1, size // 14)
    for i in range(6):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 2) % 6]
        d.line([(x1, y1), (x2, y2)], fill=(16, 163, 127, 255), width=lw)
    # draw the hexagon outline again on top
    for i in range(6):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % 6]
        d.line([(x1, y1), (x2, y2)], fill=(16, 163, 127, 255), width=lw)
    return img


def make_icon(sp=1.0, wp=1.0, sz=64, provider="claude"):
    """Generate a system-tray icon for Claude or OpenAI."""
    if provider == "openai":
        logo = _load_logo("openai-icon.png", sz)
        if logo:
            return logo
        # fallback: green circle
        img = Image.new('RGBA', (sz, sz), (0, 0, 0, 0))
        ImageDraw.Draw(img).ellipse([4, 4, sz-4, sz-4], fill=(16, 163, 127, 255))
        return img

    img = Image.new('RGBA', (sz, sz), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    logo = _load_logo("claude-logo.png", sz)
    if logo:
        img.paste(logo, (0, 0), logo)
    else:
        d.ellipse([4, 4, sz - 4, sz - 4], fill=(217, 119, 87, 255))
    return img


# ─────────────────────────────────────────────
# OpenAI Codex data fetcher
# ─────────────────────────────────────────────

class CodexDataFetcher:
    """Fetch usage data from the OpenAI Codex account (~/.codex/)."""

    CODEX_DIR = Path.home() / ".codex"
    # Same endpoint the ChatGPT web dashboard uses: the quota is per account and
    # local rollouts go stale (newer builds write .jsonl.zst, pi writes none).
    USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"

    @staticmethod
    def _empty():
        return {
            "provider": "Codex", "plan": "Plus",
            "updated": "Never", "source": "none",
            "session_used_pct": 0, "session_reset": "unknown", "session_reset_at": None,
            "session_observed_at": None, "weekly_used_pct": 0, "weekly_reset": "unknown",
            "weekly_reset_at": None, "weekly_observed_at": None,
            "session_window_seconds": None, "weekly_window_seconds": None,
            "cost_today": 0, "cost_today_tokens": "0", "cost_today_known": False,
            "cost_30d": 0, "cost_30d_tokens": "0",
            "model": "",
            "error": None, "available": False,
        }
    def fetch_quota(self):
        """Fetch only live quota for the one-minute activity sampler."""
        if not self.CODEX_DIR.exists():
            return None
        data = self._empty()
        data["available"] = True
        if not self._fetch_live_usage(data):
            return None
        if any(data.get(key) is None for key in (
                "session_used_pct", "session_reset_at", "session_observed_at", "session_window_seconds",
                "weekly_used_pct", "weekly_reset_at", "weekly_observed_at", "weekly_window_seconds")):
            return None
        data["source"] = "api"
        data["updated"] = datetime.now().strftime("Updated %H:%M")
        return data


    def fetch(self):
        d = self._empty()

        if not self.CODEX_DIR.exists():
            d["error"] = "Codex not installed"
            return d

        d["available"] = True

        # read config for model
        try:
            config = self.CODEX_DIR / "config.toml"
            if config.exists():
                for line in config.read_text().splitlines():
                    if line.startswith("model"):
                        d["model"] = line.split("=", 1)[1].strip().strip('"')
                        break
        except Exception:
            pass

        # read auth for plan type
        # read auth for plan type
        try:
            tokens = json.loads((self.CODEX_DIR / "auth.json").read_text(encoding="utf-8")).get("tokens", {})
            plan = self._openai_claims(tokens.get("access_token", "")).get("chatgpt_plan_type", "")
            if plan:
                d["plan"] = plan.capitalize()
        except Exception:
            pass

        # scan session JSONL files for rate_limits + token usage
        self._scan_sessions(d)

        # live quota wins: the local snapshot above is only a fallback
        if self._fetch_live_usage(d):
            d["source"] = "api"

        # pi agent is where the usage really happens: priced in its own logs
        self._pi_cost(d)

        d["updated"] = datetime.now().strftime("Updated %H:%M")
        return d
    def _fetch_live_usage(self, d):
        """Overwrite the 5h/weekly quota with the live account answer."""
        try:
            tokens = json.loads((self.CODEX_DIR / "auth.json").read_text(encoding="utf-8"))["tokens"]
            usage = self._usage_request(tokens)
        except Exception:
            return False  # offline or token unusable: keep the local snapshot

        rl = usage.get("rate_limit") or {}
        observed_at = time.time()
        for timing_key in ("session_reset_at", "session_observed_at", "session_window_seconds",
                           "weekly_reset_at", "weekly_observed_at", "weekly_window_seconds"):
            d[timing_key] = None
        for key, pct, reset, reset_at_key, observed_key, duration_key in (
            ("primary_window", "session_used_pct", "session_reset", "session_reset_at", "session_observed_at", "session_window_seconds"),
            ("secondary_window", "weekly_used_pct", "weekly_reset", "weekly_reset_at", "weekly_observed_at", "weekly_window_seconds"),
        ):
            window = rl.get(key) or {}
            if window:
                d[pct] = int(window.get("used_percent", 0))
                stamp = _reset_timestamp(window.get("reset_at"))
                d[reset_at_key] = stamp
                d[duration_key] = _window_seconds(window)
                d[observed_key] = observed_at if stamp else None
                if stamp:
                    d[reset] = self._format_reset(stamp)
        if usage.get("plan_type"):
            d["plan"] = usage["plan_type"].capitalize()
        return True

    def _usage_request(self, tokens):
        """GET the live quota; any failure means falling back to local data.

        No token refresh here on purpose: the stored refresh_token rotates, so
        minting one without persisting it would break the next codex run.
        """
        access = tokens.get("access_token", "")
        req = Request(self.USAGE_URL, headers={
            "Authorization": f"Bearer {access}",
            "chatgpt-account-id": tokens.get("account_id")
            or self._openai_claims(access).get("chatgpt_account_id", ""),
            "User-Agent": "codex-cli",
            "Accept": "application/json",
        })
        with urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode())

    @staticmethod
    def _openai_claims(access_token):
        """The https://api.openai.com/auth claims of an OAuth access token."""
        try:
            parts = access_token.split(".")
            if len(parts) < 2:
                return {}
            payload = parts[1] + "=" * (4 - len(parts[1]) % 4)
            return json.loads(base64.b64decode(payload)).get("https://api.openai.com/auth", {})
        except Exception:
            return {}

    def _scan_sessions(self, d):
        """Scan ~/.codex/sessions/ JSONL files for rate limits and tokens."""
        sessions_dir = self.CODEX_DIR / "sessions"
        if not sessions_dir.exists():
            d["source"] = "config"
            return

        # find all JSONL rollout files
        jsonl_files = sorted(sessions_dir.rglob("*.jsonl"),
                             key=lambda f: f.stat().st_mtime, reverse=True)
        if not jsonl_files:
            d["source"] = "config"
            return

        # shortcut: .jsonl.zst rollouts are not read (no zstd in stdlib), so this
        # quota fallback can be stale; upgrade if codex stops writing plain jsonl
        skipped = len(list(sessions_dir.rglob("*.jsonl.zst")))
        print(f"    Codex: scanning {len(jsonl_files)} session files"
              + (f" ({skipped} compressed rollouts skipped)" if skipped else ""))

        # get rate limits from most recent file (last token_count entry)
        latest_limits = None
        latest_limits_observed_at = None
        for jf in jsonl_files[:5]:  # check latest 5 files
            limits = self._extract_rate_limits(jf)
            if limits:
                latest_limits, latest_limits_observed_at = limits
                break

        if latest_limits:
            rl = latest_limits
            # primary = 5h window
            primary = rl.get("primary", {})
            if primary:
                d["session_used_pct"] = int(primary.get("used_percent", 0))
                d["session_window_seconds"] = _window_seconds(primary)
                resets_at = primary.get("resets_at")
                if resets_at:
                    d["session_reset"] = self._format_reset(resets_at)
                    d["session_reset_at"] = _reset_timestamp(resets_at)
                    d["session_observed_at"] = latest_limits_observed_at

            # secondary = weekly window
            secondary = rl.get("secondary", {})
            if secondary:
                d["weekly_used_pct"] = int(secondary.get("used_percent", 0))
                d["weekly_window_seconds"] = _window_seconds(secondary)
                resets_at = secondary.get("resets_at")
                if resets_at:
                    d["weekly_reset"] = self._format_reset(resets_at)
                    d["weekly_reset_at"] = _reset_timestamp(resets_at)
                    d["weekly_observed_at"] = latest_limits_observed_at

            plan = rl.get("plan_type", "")
            if plan:
                d["plan"] = plan.capitalize()
            d["source"] = "sessions"
        else:
            d["source"] = "config"

    @classmethod
    def _pi_cost(cls, d):
        """Cost and tokens from pi agent's own session logs.

        pi prices every assistant message it stores (message.usage.cost.total),
        so this is a measurement rather than an estimate.
        """
        sessions = Path.home() / '.pi' / 'agent' / 'sessions'
        since = datetime.now().date() - timedelta(days=30)
        today = datetime.now().date().isoformat()
        today_cost = total_cost = 0.0
        today_tokens = total_tokens = 0

        for path in sessions.rglob('*.jsonl') if sessions.exists() else ():
            try:
                if datetime.fromtimestamp(path.stat().st_mtime).date() < since:
                    continue
                with open(path, encoding='utf-8', errors='ignore') as fh:
                    for line in fh:
                        if '"cost"' not in line:
                            continue
                        try:
                            entry = json.loads(line)
                        except Exception:
                            continue
                        usage = (entry.get('message') or {}).get('usage') or {}
                        spent = (usage.get('cost') or {}).get('total')
                        if spent is None:
                            continue
                        tokens = usage.get('totalTokens') or 0
                        total_cost += spent
                        total_tokens += tokens
                        if str(entry.get('timestamp', ''))[:10] == today:
                            today_cost += spent
                            today_tokens += tokens
            except OSError:
                continue

        d["cost_today"] = round(today_cost, 2)
        d["cost_today_tokens"] = cls._fmt_tokens(today_tokens)
        d["cost_today_known"] = today_tokens > 0
        d["cost_30d"] = round(total_cost, 2)
        d["cost_30d_tokens"] = cls._fmt_tokens(total_tokens)

    @staticmethod
    def _fmt_tokens(n):
        if n >= 1e6: return f"{n / 1e6:.1f}M"
        if n >= 1e3: return f"{n / 1e3:.0f}K"
        return str(n)

    @staticmethod
    def _extract_rate_limits(jsonl_path):
        """Return (rate_limits, event timestamp) from the latest quota event."""
        last = None
        try:
            with open(jsonl_path, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or "rate_limits" not in line:
                        continue
                    try:
                        e = json.loads(line)
                        p = e.get("payload", {})
                        if isinstance(p, dict) and p.get("type") == "token_count":
                            rl = p.get("rate_limits")
                            if rl:
                                last = (rl, _reset_timestamp(e.get("timestamp")))
                    except Exception:
                        pass
        except Exception:
            pass
        return last


    @staticmethod
    def _format_reset(epoch):
        """Convert epoch timestamp to human-readable countdown."""
        try:
            dt = datetime.fromtimestamp(epoch)
            delta = dt - datetime.now()
            secs = max(0, int(delta.total_seconds()))
            h, m = divmod(secs // 60, 60)
            if h >= 24:
                return f"{h // 24}d {h % 24}h"
            return f"{h}h {m:02d}m"
        except Exception:
            return "unknown"


# ─────────────────────────────────────────────
# Native popup window — Multi-provider
# ─────────────────────────────────────────────

class CodexBarPopup(ctk.CTkToplevel):
    """Borderless popup with Claude + OpenAI tabs and smooth transitions."""

    WIDTH = 370
    FINAL_ALPHA = 1.0

    # ── Claude palette ──
    CL_BG       = "#FFFFFF"
    CL_SURFACE  = "#FAF9F7"
    CL_PRIMARY  = "#191918"
    CL_SECOND   = "#6F6E77"
    CL_TERTIARY = "#A8A7B0"
    CL_ACCENT   = "#D97757"
    CL_LITE     = "#FCEEE8"
    CL_BADGE_FG = "#C25B3B"
    CL_BADGE_BG = "#FDF0EB"
    CL_TRACK    = "#F0EFED"
    CL_DIVIDER  = "#ECEAE6"
    CL_HOVER    = "#F5F3EF"

    # ── OpenAI palette ──
    OA_BG       = "#212121"
    OA_SURFACE  = "#2A2A2A"
    OA_PRIMARY  = "#ECECEC"
    OA_SECOND   = "#A0A0A0"
    OA_TERTIARY = "#A0A0A0"
    OA_GREEN    = "#10A37F"
    OA_GREEN_LT = "#1A3A2F"
    OA_TRACK    = "#3A3A3A"
    OA_DIVIDER  = "#333333"
    OA_HOVER    = "#333333"
    OA_CARD     = "#2F2F2F"

    def __init__(self, master, claude_data, codex_data=None, *,
                 on_close=None, on_refresh=None, on_quit=None,
                 on_tab_switch=None, initial_tab="claude", quota_history=None, claude_quota_history=None):
        super().__init__(master)
        self._claude = claude_data
        self._codex = codex_data or CodexDataFetcher._empty()
        self._histories = {"openai": quota_history or {"session": [], "weekly": []},
                           "claude": claude_quota_history or {"session": [], "weekly": []}}
        self._charts = {}
        self._quota_bar_refs = {}
        self._forecast_labels = {}
        self._on_close = on_close
        self._on_refresh = on_refresh
        self._on_quit = on_quit
        self._on_tab_switch = on_tab_switch
        # The UI is always built in the Claude layout first; the requested
        # initial tab is applied right after _build_ui() via _do_swap().
        self._active_tab = "claude"
        self._initial_tab = "openai" if initial_tab == "openai" else "claude"

        self.overrideredirect(True)
        self.configure(fg_color=self.CL_BG)
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.0)

        # load logos — tiny for tab buttons, bigger for panel headers
        cl_tab = _load_logo("claude-logo.png", 18)
        self._cl_tab_icon = ctk.CTkImage(cl_tab, size=(18, 18)) if cl_tab else None
        oa_tab = _load_logo("openai-icon.png", 18)
        self._oa_tab_icon = ctk.CTkImage(oa_tab, size=(18, 18)) if oa_tab else None

        cl_big = _load_logo("claude-logo.png", 32)
        self._cl_logo_big = ctk.CTkImage(cl_big, size=(32, 32)) if cl_big else None
        oa_big = _load_logo("openai-icon.png", 28)
        self._oa_logo_big = ctk.CTkImage(oa_big, size=(28, 28)) if oa_big else None

        self._build_ui()

        # Open directly on the Codex tab when requested (e.g. Claude Code
        # is not installed but Codex is).  The window is still invisible
        # here (alpha 0), so the swap does not need the fade animation.
        if self._initial_tab != self._active_tab:
            self._active_tab = self._initial_tab
            self._do_swap()

        self.update_idletasks()
        work = self._work_area()
        w = self.WIDTH
        tab_h = self._tab_bar.winfo_reqheight()
        foot_h = self._footer_frame.winfo_reqheight()
        h = tab_h + self._fixed_panel_h + foot_h
        self._target_x = max(work[2] + 8, work[0] - w - 12)
        self._target_y = max(work[3] + 8, work[1] - h - 12)
        self.geometry(f"{w}x{h}+{self._target_x}+{self._target_y + 14}")

        self.after(30, self._apply_dwm)
        self.bind("<Escape>", lambda e: self._close())
        self.bind("<FocusOut>", self._on_focus_out)
        self.focus_force()
        self.after(40, self._animate_in, 0)

    # ── DWM ──

    def _work_area(self):
        """Return (right, bottom, left, top) of the usable screen area
        in logical pixels (what customtkinter geometry() expects).

        customtkinter sets Per-Monitor DPI awareness, so Win32 APIs
        return physical pixels.  Divide by the CTk scaling factor
        to convert to the logical coordinate space that geometry() uses.
        """
        try:
            from ctypes import wintypes
            rect = wintypes.RECT()
            ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(rect), 0)
            scale = self._get_window_scaling()
            return (int(rect.right / scale), int(rect.bottom / scale),
                    int(rect.left / scale), int(rect.top / scale))
        except Exception:
            return (self.winfo_screenwidth(), self.winfo_screenheight(), 0, 0)

    def _apply_dwm(self):
        try:
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            pref = ctypes.c_int(2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
            class MARGINS(ctypes.Structure):
                _fields_ = [("l", ctypes.c_int), ("r", ctypes.c_int),
                            ("t", ctypes.c_int), ("b", ctypes.c_int)]
            m = MARGINS(0, 0, 1, 0)
            ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(m))
        except Exception:
            pass

    # ── animation ──

    def _animate_in(self, step, total=14):
        if step > total:
            return
        t = step / total
        ease = 1.0 - (1.0 - t) ** 3
        y = int(self._target_y + 18 * (1.0 - ease))
        alpha = min(ease * 1.0, self.FINAL_ALPHA)
        try:
            self.geometry(f"+{self._target_x}+{y}")
            self.attributes("-alpha", alpha)
            self.after(14, self._animate_in, step + 1, total)
        except Exception:
            pass

    # ── focus ──

    def _on_focus_out(self, event):
        self.after(120, self._check_focus)

    def _check_focus(self):
        try:
            fw = self.focus_get()
            if fw is not None and str(fw).startswith(str(self)):
                return
        except Exception:
            pass
        self._close()

    # ── Apple-style tab transition ──
    #
    # Only animate alpha (GPU-accelerated via DWM).
    # Phase 1  "out": ease-in  alpha 0.94 → 0.0   (~100ms)
    # Instant:  swap content, colors, resize, reposition
    # Phase 2  "in":  ease-out alpha 0.0 → 0.94  (~180ms) + 8px slide-up

    def _switch_tab(self, tab):
        if tab == self._active_tab:
            return
        self._active_tab = tab
        if self._on_tab_switch:
            self._on_tab_switch(tab)
        self._m_step = 0
        self._m_phase = "out"
        self._morph_tick()

    def _do_swap(self):
        """Instant swap while window is invisible."""
        tab = self._active_tab

        # tab button + footer styles
        if tab == "claude":
            self._tab_bar.configure(fg_color=self.CL_BG)
            self._tab_inner.configure(fg_color=self.CL_TRACK)
            self._cl_tab_btn.configure(fg_color=self.CL_LITE, hover_color=self.CL_LITE)
            self._oa_tab_btn.configure(fg_color="transparent", hover_color=self.CL_HOVER)
            self.configure(fg_color=self.CL_BG)
            self._footer_frame.configure(fg_color=self.CL_BG)
            self._footer_divider.configure(fg_color=self.CL_DIVIDER)
            self._dash_btn.configure(text_color=self.CL_ACCENT, hover_color=self.CL_HOVER)
            self._quit_btn.configure(text_color=self.CL_TERTIARY, hover_color=self.CL_HOVER)
            self._refresh_btn.configure(fg_color=self.CL_ACCENT, hover_color="#C4654A")
        else:
            self._tab_bar.configure(fg_color=self.OA_BG)
            self._tab_inner.configure(fg_color=self.OA_TRACK)
            self._cl_tab_btn.configure(fg_color="transparent", hover_color=self.OA_HOVER)
            self._oa_tab_btn.configure(fg_color=self.OA_GREEN_LT, hover_color=self.OA_GREEN_LT)
            self.configure(fg_color=self.OA_BG)
            self._footer_frame.configure(fg_color=self.OA_BG)
            self._footer_divider.configure(fg_color=self.OA_DIVIDER)
            self._dash_btn.configure(text_color=self.OA_GREEN, hover_color=self.OA_HOVER)
            self._quit_btn.configure(text_color=self.OA_TERTIARY, hover_color=self.OA_HOVER)
            self._refresh_btn.configure(fg_color=self.OA_GREEN, hover_color="#0D8A6A")

        # swap frames
        self._claude_frame.pack_forget()
        self._openai_frame.pack_forget()
        self._footer_frame.pack_forget()
        if tab == "claude":
            self._claude_frame.pack(fill="both", expand=True)
        else:
            self._openai_frame.pack(fill="both", expand=True)
        self._footer_frame.pack(fill="x", side="bottom")

        # resize to fixed height + reposition anchored to bottom
        self.update_idletasks()
        tab_h = self._tab_bar.winfo_reqheight()
        foot_h = self._footer_frame.winfo_reqheight()
        h = tab_h + self._fixed_panel_h + foot_h
        work = self._work_area()
        self._target_x = max(work[2] + 8, work[0] - self.WIDTH - 12)
        self._target_y = max(work[3] + 8, work[1] - h - 12)
        self.geometry(f"{self.WIDTH}x{h}+{self._target_x}+{self._target_y}")

    def _morph_tick(self):
        try:
            if self._m_phase == "out":
                # Fade out: 7 steps × 14ms = ~100ms, ease-in (accelerate)
                total = 7
                s = self._m_step
                if s >= total:
                    # hide: alpha=0 + move off-screen to prevent any flash
                    self.attributes("-alpha", 0.0)
                    self.geometry(f"+{self._target_x}+-9999")
                    self._do_swap()
                    # window is now resized off-screen, invisible
                    self.attributes("-alpha", 0.0)
                    self._m_step = 0
                    self._m_phase = "in"
                    self.after(20, self._morph_tick)
                    return
                t = s / total
                ease = t * t  # ease-in
                alpha = self.FINAL_ALPHA * (1.0 - ease)
                self.attributes("-alpha", max(alpha, 0.0))
                self._m_step += 1
                self.after(14, self._morph_tick)

            elif self._m_phase == "in":
                # Fade in: 12 steps × 14ms = ~170ms, ease-out + slide up 8px
                total = 12
                s = self._m_step
                if s >= total:
                    self.attributes("-alpha", self.FINAL_ALPHA)
                    self.geometry(f"+{self._target_x}+{self._target_y}")
                    return
                t = s / total
                ease = 1.0 - (1.0 - t) ** 3  # ease-out (decelerate)
                alpha = self.FINAL_ALPHA * ease
                # subtle slide up
                y_off = int(8 * (1.0 - ease))
                self.attributes("-alpha", alpha)
                self.geometry(f"+{self._target_x}+{self._target_y + y_off}")
                self._m_step += 1
                self.after(14, self._morph_tick)
        except Exception:
            pass

    # ── bar colour helpers ──


    def _usage_forecast(self, parent, used, reset_at, observed_at, window_seconds, text_color):
        text, color = _quota_forecast_display(used, reset_at, observed_at,
                                             window_seconds, text_color)
        label = ctk.CTkLabel(parent, text=text, font=("Segoe UI", 10),
                             text_color=color, anchor="w", wraplength=300,
                             justify="left")
        label.pack(fill="x", padx=2, pady=(3, 1))
        return label

    @staticmethod
    def _cl_bar_color(pct):
        if pct <= 50:  return "#D97757"
        if pct <= 80:  return "#E8943E"
        return "#D94A3D"

    @staticmethod
    def _oa_bar_color(pct):
        if pct <= 50:  return "#10A37F"
        if pct <= 80:  return "#E8A83E"
        return "#E24B4A"

    # ═══════════════════════════════════════
    # MAIN UI BUILD
    # ═══════════════════════════════════════

    def _build_ui(self):
        # ── TAB BAR — tiny icon pills, top-left ──
        tab_bar = ctk.CTkFrame(self, fg_color=self.CL_BG, corner_radius=0, height=34)
        tab_bar.pack(fill="x")
        tab_bar.pack_propagate(False)
        self._tab_bar = tab_bar

        self._tab_inner = ctk.CTkFrame(tab_bar, fg_color=self.CL_TRACK, corner_radius=9)
        self._tab_inner.pack(side="left", padx=14, pady=4)
        tab_inner = self._tab_inner

        self._cl_tab_btn = ctk.CTkButton(
            tab_inner,
            text="",
            image=self._cl_tab_icon,
            font=("Segoe UI", 1),
            fg_color=self.CL_LITE,
            hover_color=self.CL_LITE,
            corner_radius=8, height=26, width=34,
            command=lambda: self._switch_tab("claude"))
        self._cl_tab_btn.pack(side="left", padx=(2, 1), pady=2)

        self._oa_tab_btn = ctk.CTkButton(
            tab_inner,
            text="",
            image=self._oa_tab_icon,
            font=("Segoe UI", 1),
            fg_color="transparent",
            hover_color=self.CL_HOVER,
            corner_radius=8, height=26, width=34,
            command=lambda: self._switch_tab("openai"))
        self._oa_tab_btn.pack(side="left", padx=(1, 2), pady=2)

        # ── CLAUDE CONTENT ──
        self._claude_frame = ctk.CTkFrame(self, fg_color=self.CL_BG, corner_radius=0)
        self._build_claude_panel(self._claude_frame)
        self._claude_frame.pack(fill="both", expand=True)

        # ── OPENAI CONTENT ──
        self._openai_frame = ctk.CTkFrame(self, fg_color=self.OA_BG, corner_radius=0)
        self._build_openai_panel(self._openai_frame)
        # starts hidden

        # ── measure both panel heights to equalize later ──
        self.update_idletasks()
        ch = self._claude_frame.winfo_reqheight()
        # temporarily pack openai to measure it
        self._openai_frame.pack(fill="both", expand=True)
        self.update_idletasks()
        oh = self._openai_frame.winfo_reqheight()
        self._openai_frame.pack_forget()
        # store the fixed popup height (tab_bar + max_panel + footer)
        self._fixed_panel_h = max(ch, oh)

        # ── FOOTER (always visible, bottom) ──
        self._footer_frame = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0)
        self._build_footer(self._footer_frame)
        self._footer_frame.pack(fill="x", side="bottom")

    # ═══════════════════════════════════════
    # CLAUDE PANEL
    # ═══════════════════════════════════════

    def _build_claude_panel(self, parent):
        d = self._claude
        sp = d["session_used_pct"]
        wp = d["weekly_used_pct"]
        op = d["opus_used_pct"]
        has_data = d["source"] != "none"
        # logs only carry the cost: without cli/api the quota is unknown, not 0
        has_quota = d["source"] in ("cli", "api")
        has_cost = d["cost_today"] > 0 or d["cost_30d"] > 0

        # warm gradient flare
        for color, h in [
            ("#FCEEE8", 4), ("#FDF1EC", 3), ("#FDF4F0", 3),
            ("#FEF6F3", 3), ("#FEF8F6", 2), ("#FFFCFB", 2),
        ]:
            ctk.CTkFrame(parent, fg_color=color, height=h,
                         corner_radius=0).pack(fill="x")

        # header
        hero = ctk.CTkFrame(parent, fg_color="transparent")
        hero.pack(fill="x", padx=22, pady=(4, 0))

        row = ctk.CTkFrame(hero, fg_color="transparent")
        row.pack(fill="x")
        if self._cl_logo_big:
            ctk.CTkLabel(row, text="", image=self._cl_logo_big,
                         width=32, height=32).pack(side="left", padx=(0, 10))
        ctk.CTkLabel(row, text="Claude", font=("Segoe UI Semibold", 22),
                     text_color=self.CL_PRIMARY).pack(side="left")
        ctk.CTkLabel(row, text=f"  {d['plan']}  ", font=("Segoe UI Semibold", 11),
                     text_color=self.CL_BADGE_FG, fg_color=self.CL_BADGE_BG,
                     corner_radius=10).pack(side="right")

        meta = ctk.CTkFrame(hero, fg_color="transparent")
        meta.pack(fill="x", pady=(5, 0))
        ctk.CTkFrame(meta, fg_color="#5CB176", corner_radius=4,
                     width=7, height=7).pack(side="left", padx=(1, 7), pady=5)
        ctk.CTkLabel(meta, text=d["updated"], font=("Segoe UI", 12),
                     text_color=self.CL_SECOND).pack(side="left")
        ctk.CTkLabel(meta, text=f"  {d['source']}", font=("Segoe UI", 11),
                     text_color=self.CL_TERTIARY).pack(side="left")

        if has_data:
            ctk.CTkFrame(parent, fg_color=self.CL_DIVIDER,
                         height=1, corner_radius=0).pack(fill="x", padx=20, pady=(12, 0))
            ctk.CTkLabel(parent, text="Usage", font=("Segoe UI Semibold", 13),
                         text_color=self.CL_TERTIARY,
                         anchor="w").pack(fill="x", padx=22, pady=(10, 2))
            self._cl_usage_bar(parent, "Session", sp if has_quota else None, d["session_reset"], d.get("session_window_seconds"), d.get("session_reset_at"), d.get("session_observed_at"))
            self._cl_usage_bar(parent, "Weekly", wp if has_quota else None, d["weekly_reset"], d.get("weekly_window_seconds"), d.get("weekly_reset_at"), d.get("weekly_observed_at"))
            if op > 0:
                self._cl_usage_bar(parent, "Opus", op)

        if has_data:
            self._build_activity_chart(parent, "claude")

        if has_cost:
            ctk.CTkFrame(parent, fg_color=self.CL_DIVIDER,
                         height=1, corner_radius=0).pack(fill="x", padx=20, pady=(8, 0))
            ctk.CTkLabel(parent, text="API Cost Estimate",
                         font=("Segoe UI Semibold", 13),
                         text_color=self.CL_TERTIARY,
                         anchor="w").pack(fill="x", padx=22, pady=(10, 0))
            ctk.CTkLabel(parent, text="Estimated API equivalent — not billed",
                         font=("Segoe UI", 10),
                         text_color=self.CL_TERTIARY,
                         anchor="w").pack(fill="x", padx=22, pady=(0, 4))
            card = ctk.CTkFrame(parent, fg_color=self.CL_SURFACE, corner_radius=10)
            card.pack(fill="x", padx=20, pady=(0, 2))
            inner = ctk.CTkFrame(card, fg_color="transparent")
            inner.pack(fill="x", padx=14, pady=10)
            for label, val in [("Today", _cost_today_text(d)),
                               ("Last 30 days", f"${d['cost_30d']:.2f}")]:
                r = ctk.CTkFrame(inner, fg_color="transparent")
                r.pack(fill="x", pady=1)
                ctk.CTkLabel(r, text=label, font=("Segoe UI", 12),
                             text_color=self.CL_SECOND).pack(side="left")
                ctk.CTkLabel(r, text=val, font=("Segoe UI Semibold", 13),
                             text_color=self.CL_PRIMARY).pack(side="right")

        if not d.get("installed", True) and not has_data and not has_cost:
            ctk.CTkFrame(parent, fg_color=self.CL_DIVIDER,
                         height=1, corner_radius=0).pack(fill="x", padx=20, pady=(12, 0))
            nd = ctk.CTkFrame(parent, fg_color="transparent")
            nd.pack(fill="x", padx=20, pady=(20, 8))
            ctk.CTkLabel(nd, text="Claude Code not detected",
                         font=("Segoe UI Semibold", 14),
                         text_color=self.CL_PRIMARY).pack(pady=(0, 4))
            ctk.CTkLabel(nd, text="Install the CLI to see your usage",
                         font=("Segoe UI", 11),
                         text_color=self.CL_SECOND).pack(pady=(0, 12))
            ctk.CTkButton(nd, text="Install Claude Code",
                          font=("Segoe UI Semibold", 13),
                          text_color="#FFFFFF", fg_color=self.CL_ACCENT,
                          hover_color="#C4654A", corner_radius=10,
                          height=38, width=200,
                          command=lambda: self._open_url(
                              "https://docs.anthropic.com/en/docs/claude-code/overview")
                          ).pack()
        elif not has_data and not has_cost:
            nd = ctk.CTkFrame(parent, fg_color="transparent")
            nd.pack(fill="x", padx=20, pady=24)
            ctk.CTkLabel(nd, text="No session data yet", font=("Segoe UI", 13),
                         text_color=self.CL_SECOND).pack()
            ctk.CTkLabel(nd, text="Run /usage in Claude Code",
                         font=("Segoe UI", 11),
                         text_color=self.CL_TERTIARY).pack(pady=(4, 0))

        ctk.CTkFrame(parent, fg_color="transparent", height=6).pack(fill="x")

    def _cl_usage_bar(self, parent, label, used, reset=None, window_seconds=None, reset_at=None, observed_at=None):
        left = None if used is None else 100 - used
        color = self.CL_TERTIARY if left is None else self._cl_bar_color(used)
        sec = ctk.CTkFrame(parent, fg_color="transparent")
        sec.pack(fill="x", padx=20, pady=(3, 2))
        row = ctk.CTkFrame(sec, fg_color="transparent")
        row.pack(fill="x")
        ctk.CTkLabel(row, text=label, font=("Segoe UI Semibold", 13),
                     text_color=self.CL_PRIMARY).pack(side="left")
        remaining_label = ctk.CTkLabel(row, text="—" if left is None else f"{left}% left",
                                        font=("Segoe UI Semibold", 13), text_color=color)
        remaining_label.pack(side="right")
        track = ctk.CTkFrame(sec, fg_color=self.CL_TRACK, height=8, corner_radius=4)
        track.pack(fill="x", pady=(4, 3))
        track.pack_propagate(False)
        fill = ctk.CTkFrame(track, fg_color=color, corner_radius=4, height=8)
        fill.place(relx=0, rely=0, relwidth=0 if left is None else max(left / 100, 0.015), relheight=1)
        reset_label = None
        if reset and reset != "unknown":
            reset_label = ctk.CTkLabel(sec, text=f"Resets {reset}", font=("Segoe UI", 11),
                                       text_color=self.CL_SECOND, anchor="w")
            reset_label.pack(fill="x")
        if label != "Opus" and used is not None:
            key = "weekly" if label == "Weekly" else "session"
            self._quota_bar_refs[key] = (remaining_label, fill, reset_label)
            self._forecast_labels[key] = self._usage_forecast(
                sec, used, reset_at, observed_at, window_seconds, self.CL_SECOND)
    # ═══════════════════════════════════════
    # OPENAI PANEL — mirrors Claude layout
    # ═══════════════════════════════════════

    def _build_openai_panel(self, parent):
        d = self._codex
        self._quota_bar_refs = {}
        self._forecast_labels = {}
        available = d.get("available", False)
        sp = d["session_used_pct"]
        wp = d["weekly_used_pct"]
        has_data = d["source"] not in ("none", "config")
        has_cost = d["cost_today"] > 0 or d["cost_30d"] > 0

        # subtle dark gradient flare (dark → slightly lighter → dark)
        for color, h in [
            ("#2A2E2C", 4), ("#282C2A", 3), ("#262A28", 3),
            ("#252826", 3), ("#242725", 2), ("#232524", 2),
        ]:
            ctk.CTkFrame(parent, fg_color=color, height=h,
                         corner_radius=0).pack(fill="x")

        # header
        hero = ctk.CTkFrame(parent, fg_color="transparent")
        hero.pack(fill="x", padx=22, pady=(4, 0))

        row = ctk.CTkFrame(hero, fg_color="transparent")
        row.pack(fill="x")
        if self._oa_logo_big:
            ctk.CTkLabel(row, text="", image=self._oa_logo_big,
                         width=28, height=28).pack(side="left", padx=(0, 10))
        ctk.CTkLabel(row, text="Codex", font=("Segoe UI Semibold", 22),
                     text_color=self.OA_PRIMARY).pack(side="left")
        plan_text = d["plan"]
        if d["model"]:
            plan_text = d["model"]
        ctk.CTkLabel(row, text=f"  {plan_text}  ", font=("Segoe UI Semibold", 11),
                     text_color=self.OA_GREEN, fg_color=self.OA_GREEN_LT,
                     corner_radius=10).pack(side="right")

        meta = ctk.CTkFrame(hero, fg_color="transparent")
        meta.pack(fill="x", pady=(5, 0))
        dot_color = self.OA_GREEN if available else self.OA_TERTIARY
        ctk.CTkFrame(meta, fg_color=dot_color, corner_radius=4,
                     width=7, height=7).pack(side="left", padx=(1, 7), pady=5)
        ctk.CTkLabel(meta, text=d["updated"], font=("Segoe UI", 12),
                     text_color=self.OA_SECOND).pack(side="left")
        ctk.CTkLabel(meta, text=f"  {d['source']}", font=("Segoe UI", 11),
                     text_color=self.OA_TERTIARY).pack(side="left")
        if available:
            ctk.CTkLabel(hero, text="Scadenza abbonamento: non disponibile nei dati account",
                         font=("Segoe UI", 10), text_color=self.OA_SECOND,
                         anchor="w").pack(fill="x", pady=(2, 0))

        if not available:
            ctk.CTkFrame(parent, fg_color=self.OA_DIVIDER,
                         height=1, corner_radius=0).pack(fill="x", padx=20, pady=(12, 0))
            nd = ctk.CTkFrame(parent, fg_color="transparent")
            nd.pack(fill="x", padx=20, pady=(20, 8))
            ctk.CTkLabel(nd, text="Codex not detected",
                         font=("Segoe UI Semibold", 14),
                         text_color=self.OA_PRIMARY).pack(pady=(0, 4))
            ctk.CTkLabel(nd, text="Install the CLI to see your usage",
                         font=("Segoe UI", 11),
                         text_color=self.OA_SECOND).pack(pady=(0, 12))
            ctk.CTkButton(nd, text="Install Codex",
                          font=("Segoe UI Semibold", 13),
                          text_color="#FFFFFF", fg_color=self.OA_GREEN,
                          hover_color="#0D8A6A", corner_radius=10,
                          height=38, width=200,
                          command=lambda: self._open_url(
                              "https://github.com/openai/codex")
                          ).pack()
            ctk.CTkFrame(parent, fg_color="transparent", height=6).pack(fill="x")
            return

        if has_data:
            ctk.CTkFrame(parent, fg_color=self.OA_DIVIDER,
                         height=1, corner_radius=0).pack(fill="x", padx=20, pady=(12, 0))
            ctk.CTkLabel(parent, text="Usage", font=("Segoe UI Semibold", 13),
                         text_color=self.OA_TERTIARY,
                         anchor="w").pack(fill="x", padx=22, pady=(10, 2))
            self._oa_usage_bar(parent, "Session (5h)", sp, d["session_reset"], d.get("session_window_seconds"), d.get("session_reset_at"), d.get("session_observed_at"))
            self._oa_usage_bar(parent, "Weekly", wp, d["weekly_reset"], d.get("weekly_window_seconds"), d.get("weekly_reset_at"), d.get("weekly_observed_at"))

        if available:
            self._build_activity_chart(parent, "openai")

        if has_cost:
            ctk.CTkFrame(parent, fg_color=self.OA_DIVIDER,
                         height=1, corner_radius=0).pack(fill="x", padx=20, pady=(8, 0))
            ctk.CTkLabel(parent, text="pi Agent Cost",
                         font=("Segoe UI Semibold", 13),
                         text_color=self.OA_TERTIARY,
                         anchor="w").pack(fill="x", padx=22, pady=(10, 0))
            ctk.CTkLabel(parent, text="Priced by pi from real token usage — not billed",
                         font=("Segoe UI", 10),
                         text_color=self.OA_TERTIARY,
                         anchor="w").pack(fill="x", padx=22, pady=(0, 4))
            card = ctk.CTkFrame(parent, fg_color=self.OA_CARD, corner_radius=10)
            card.pack(fill="x", padx=20, pady=(0, 2))
            inner = ctk.CTkFrame(card, fg_color="transparent")
            inner.pack(fill="x", padx=14, pady=10)
            for label, val in [("Today", _cost_today_text(d)),
                               ("Last 30 days", f"${d['cost_30d']:.2f}")]:
                r = ctk.CTkFrame(inner, fg_color="transparent")
                r.pack(fill="x", pady=1)
                ctk.CTkLabel(r, text=label, font=("Segoe UI", 12),
                             text_color=self.OA_SECOND).pack(side="left")
                ctk.CTkLabel(r, text=val, font=("Segoe UI Semibold", 13),
                             text_color=self.OA_PRIMARY).pack(side="right")

        if not has_data and not has_cost:
            ctk.CTkFrame(parent, fg_color=self.OA_DIVIDER,
                         height=1, corner_radius=0).pack(fill="x", padx=20, pady=(12, 0))
            nd = ctk.CTkFrame(parent, fg_color="transparent")
            nd.pack(fill="x", padx=20, pady=24)
            ctk.CTkLabel(nd, text="No session data yet", font=("Segoe UI", 13),
                         text_color=self.OA_SECOND).pack()
            ctk.CTkLabel(nd, text="Run a session in Codex CLI",
                         font=("Segoe UI", 11),
                         text_color=self.OA_TERTIARY).pack(pady=(4, 0))

        ctk.CTkFrame(parent, fg_color="transparent", height=6).pack(fill="x")

    def _oa_usage_bar(self, parent, label, used, reset=None, window_seconds=None, reset_at=None, observed_at=None):
        left = None if used is None else max(0, 100 - used)
        color = self.OA_TERTIARY if left is None else self._oa_bar_color(used)
        key = "weekly" if label == "Weekly" else "session"
        sec = ctk.CTkFrame(parent, fg_color="transparent")
        sec.pack(fill="x", padx=20, pady=(3, 2))
        row = ctk.CTkFrame(sec, fg_color="transparent")
        row.pack(fill="x")
        ctk.CTkLabel(row, text=label, font=("Segoe UI Semibold", 13),
                     text_color=self.OA_PRIMARY).pack(side="left")
        remaining_label = ctk.CTkLabel(row, text="—" if left is None else f"{left}% left",
                                        font=("Segoe UI Semibold", 13), text_color=color)
        remaining_label.pack(side="right")
        track = ctk.CTkFrame(sec, fg_color=self.OA_TRACK, height=8, corner_radius=4)
        track.pack(fill="x", pady=(4, 3))
        track.pack_propagate(False)
        fill = ctk.CTkFrame(track, fg_color=color, corner_radius=4, height=8)
        fill.place(relx=0, rely=0, relwidth=0 if left is None else max(left / 100, 0.015), relheight=1)
        reset_label = None
        if reset and reset != "unknown":
            reset_label = ctk.CTkLabel(sec, text=f"Resets {reset}", font=("Segoe UI", 11),
                                       text_color=self.OA_SECOND, anchor="w")
            reset_label.pack(fill="x")
        self._quota_bar_refs[key] = (remaining_label, fill, reset_label)
        if used is not None:
            self._forecast_labels[key] = self._usage_forecast(
                sec, used, reset_at, observed_at, window_seconds, self.OA_SECOND)

    def _chart_colors(self, name):
        if name == "claude":
            return {"card": self.CL_SURFACE, "primary": self.CL_PRIMARY, "second": self.CL_SECOND,
                    "divider": self.CL_DIVIDER, "hover": self.CL_HOVER, "lite": self.CL_LITE,
                    "line": self.CL_ACCENT}
        return {"card": self.OA_CARD, "primary": self.OA_PRIMARY, "second": self.OA_SECOND,
                "divider": self.OA_DIVIDER, "hover": self.OA_HOVER, "lite": self.OA_GREEN_LT,
                "line": self.OA_GREEN}

    def _build_activity_chart(self, parent, name):
        c = self._chart_colors(name)
        card = ctk.CTkFrame(parent, fg_color=c["card"], corner_radius=10)
        card.pack(fill="x", padx=20, pady=(8, 2))
        header = ctk.CTkFrame(card, fg_color="transparent")
        header.pack(fill="x", padx=12, pady=(9, 2))
        ctk.CTkLabel(header, text="Attività quota", font=("Segoe UI Semibold", 12),
                     text_color=c["primary"]).pack(side="left")
        selectors = ctk.CTkFrame(header, fg_color="transparent")
        selectors.pack(side="right")
        chart = {"window": "session", "colors": c, "buttons": {}}
        for key, title in (("session", "5h"), ("weekly", "7g")):
            button = ctk.CTkButton(
                selectors, text=title, width=42, height=25,
                fg_color=c["lite"] if key == "session" else "transparent",
                hover_color=c["hover"], text_color=c["primary"],
                corner_radius=7, command=lambda value=key: self._select_activity_window(name, value))
            button.pack(side="left", padx=2)
            chart["buttons"][key] = button
        chart["note"] = ctk.CTkLabel(card, text="", font=("Segoe UI", 10),
                                     text_color=c["second"], anchor="w",
                                     wraplength=290, justify="left")
        chart["note"].pack(fill="x", padx=14, pady=(0, 2))
        chart["canvas"] = ctk.CTkCanvas(card, height=112, bg=c["card"], highlightthickness=0)
        chart["canvas"].bind("<Configure>", lambda _event: self._draw_activity_chart(name))
        chart["canvas"].bind("<Map>", lambda _event: self._draw_activity_chart(name))
        chart["canvas"].pack(fill="x", padx=8, pady=(0, 2))
        chart["projection"] = ctk.CTkLabel(card, text="", font=("Segoe UI", 10),
                                           text_color=c["second"], anchor="w",
                                           wraplength=290, justify="left")
        chart["projection"].pack(fill="x", padx=14, pady=(0, 9))
        self._charts[name] = chart
        self._refresh_activity_chart(name)

    def _select_activity_window(self, name, key):
        chart = self._charts[name]
        chart["window"] = key
        for other, button in chart["buttons"].items():
            button.configure(fg_color=chart["colors"]["lite"] if other == key else "transparent")
        self._refresh_activity_chart(name)

    def _draw_activity_chart(self, name):
        chart = self._charts.get(name)
        if chart is None:
            return
        c, canvas = chart["colors"], chart["canvas"]
        canvas.delete("all")
        width, height = max(canvas.winfo_width(), 280), 112
        left, right, top, bottom = 30, width - 10, 10, height - 22
        plot_h = bottom - top
        for pct in (0, 50, 100):
            y = bottom - plot_h * pct / 100
            canvas.create_line(left, y, right, y, fill=c["divider"])
            canvas.create_text(left - 5, y, text=str(pct), anchor="e",
                               fill=c["second"], font=("Segoe UI", 9))
        canvas.create_text(left, height - 2, text="inizio finestra", anchor="sw",
                           fill=c["second"], font=("Segoe UI", 9))
        canvas.create_text(right, height - 2, text="ora", anchor="se",
                           fill=c["second"], font=("Segoe UI", 9))
        samples = self._histories[name].get(chart["window"], [])
        if not samples:
            canvas.create_text((left + right) / 2, (top + bottom) / 2,
                               text="In attesa di campioni quota live",
                               fill=c["second"], font=("Segoe UI", 10))
            return
        latest = samples[-1]
        start_at = latest["reset_at"] - latest["duration"]
        samples = [sample for sample in samples
                   if abs(sample["reset_at"] - latest["reset_at"]) <= 120
                   and sample["at"] >= start_at]
        points = []
        for sample in samples:
            x = left + (right - left) * min(1, max(0, (sample["at"] - start_at) / latest["duration"]))
            y = bottom - plot_h * sample["used"] / 100
            points.append((x, y, sample["at"]))
        for a, b in zip(points, points[1:]):
            if b[2] - a[2] <= 300:
                canvas.create_line(a[0], a[1], b[0], b[1], fill=c["line"], width=2)
        for x, y, _ in points:
            canvas.create_oval(x - 3, y - 3, x + 3, y + 3,
                               fill=c["line"], outline=c["line"])

    def _refresh_activity_chart(self, name):
        chart = self._charts.get(name)
        if chart is None:
            return
        window = chart["window"]
        samples = self._histories[name].get(window, [])
        chart["note"].configure(text=_quota_change_text(samples, poll="~5 min" if name == "claude" else "~1 min"))
        self._draw_activity_chart(name)
        data = self._claude if name == "claude" else self._codex
        prefix = "session" if window == "session" else "weekly"
        text, color = _quota_forecast_display(
            data.get(f"{prefix}_used_pct"), data.get(f"{prefix}_reset_at"),
            data.get(f"{prefix}_observed_at"), data.get(f"{prefix}_window_seconds"),
            chart["colors"]["second"])
        chart["projection"].configure(text=text, text_color=color)

    def update_codex_quota(self, data):
        self._codex = data
        for key, prefix in (("session", "session"), ("weekly", "weekly")):
            used = data.get(f"{prefix}_used_pct")
            if used is None or key not in self._quota_bar_refs:
                continue
            label, fill, reset_label = self._quota_bar_refs[key]
            remaining = max(0, 100 - used)
            color = self._oa_bar_color(used)
            label.configure(text=f"{remaining}% left", text_color=color)
            fill.configure(fg_color=color)
            fill.place(relx=0, rely=0, relwidth=max(remaining / 100, 0.015), relheight=1)
            if reset_label is not None and data.get(f"{prefix}_reset"):
                reset_label.configure(text=f"Resets {data[f'{prefix}_reset']}")
            forecast = self._forecast_labels.get(key)
            if forecast is not None:
                text, text_color = _quota_forecast_display(
                    used, data.get(f"{prefix}_reset_at"), data.get(f"{prefix}_observed_at"),
                    data.get(f"{prefix}_window_seconds"), self.OA_SECOND)
                forecast.configure(text=text, text_color=text_color)
        self._refresh_activity_chart("openai")

    # ═══════════════════════════════════════

    def _build_footer(self, parent):
        self._footer_divider = ctk.CTkFrame(parent, fg_color=self.CL_DIVIDER,
                     height=1, corner_radius=0)
        self._footer_divider.pack(fill="x", padx=20)

        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(6, 6))

        self._dash_btn = ctk.CTkButton(
            row, text="Dashboard", font=("Segoe UI", 12),
            text_color=self.CL_ACCENT, fg_color="transparent",
            hover_color=self.CL_HOVER, anchor="w", height=30,
            corner_radius=8, width=80,
            command=lambda: self._open_url(
                "https://chatgpt.com/settings/usage?tab=overview" if self._active_tab == "openai"
                else "https://claude.ai/settings/billing"))
        self._dash_btn.pack(side="left", padx=2)

        self._quit_btn = ctk.CTkButton(
            row, text="Quit", font=("Segoe UI", 12),
            text_color=self.CL_TERTIARY, fg_color="transparent",
            hover_color=self.CL_HOVER, anchor="center", height=30,
            corner_radius=8, width=50, command=self._do_quit)
        self._quit_btn.pack(side="right", padx=2)

        self._refresh_btn = ctk.CTkButton(
            row, text="Refresh", font=("Segoe UI Semibold", 12),
            text_color="#FFFFFF", fg_color=self.CL_ACCENT,
            hover_color="#C4654A", anchor="center", height=30,
            corner_radius=8, width=70, command=self._do_refresh)
        self._refresh_btn.pack(side="right", padx=2)

    # ── helpers ──

    def _open_url(self, url):
        webbrowser.open(url)
        self._close()

    def _close(self):
        try:
            self.destroy()
        except Exception:
            pass
        if self._on_close:
            self._on_close()

    def _do_refresh(self):
        self._close()
        if self._on_refresh:
            self._on_refresh()

    def _do_quit(self):
        self._close()
        if self._on_quit:
            self._on_quit()


# ─────────────────────────────────────────────
# App  (tkinter main loop + pystray background)
# ─────────────────────────────────────────────

class CodexBarApp:
    def __init__(self, prefer_codex=False, window_alerts=True):
        self.prefer_codex = prefer_codex
        self.fetcher = ClaudeDataFetcher()
        self.codex_fetcher = CodexDataFetcher()
        self.root = None
        self.tray = None
        self.popup = None
        self.running = True
        self.codex_data = None
        self.quota_history = {"session": [], "weekly": []}
        self.claude_history = {"session": [], "weekly": []}
        self._reset_warned = {}  # (provider, window) -> reset_at already notified
        self.window_alerts = window_alerts
        self._session_reset_seen = {}  # provider -> last 5h reset_at still in the future
        self._window_warned = {}  # (provider, event) -> reset_at already notified
        # provider currently shown in the tray / last tab opened by the user
        self.active_provider = "claude"

    @staticmethod
    def default_provider(claude_data, codex_data, prefer_codex=False):
        """Pick the provider to show at startup.

        Claude stays the default, except when the user asked for Codex
        (--codex) or Claude Code is not installed while Codex is: a
        Codex-only user should not be greeted by the Claude icon and an
        "Install Claude Code" prompt (issue #10).
        """
        claude_installed = bool((claude_data or {}).get("installed", False))
        codex_available = bool((codex_data or {}).get("available", False))
        if codex_available and (prefer_codex or not claude_installed):
            return "openai"
        return "claude"

    def start(self):
        print("[CodexBar] Fetching your real usage data...\n")
        self.fetcher.fetch_all()
        _record_quota_sample(self.claude_history, self.fetcher.data)
        print(f"\n[CodexBar] Source: {self.fetcher.data['source']}")
        try:
            self.codex_data = self.codex_fetcher.fetch()
            print(f"[CodexBar] Codex: {'available' if self.codex_data.get('available') else 'not found'}")
        except Exception as e:
            print(f"[CodexBar] Codex fetch err: {e}")
            self.codex_data = CodexDataFetcher._empty()

        _record_quota_sample(self.quota_history, self.codex_data)
        self.active_provider = self.default_provider(
            self.fetcher.data, self.codex_data, self.prefer_codex)
        print(f"[CodexBar] Default tab: {'Codex' if self.active_provider == 'openai' else 'Claude'}")

        # ── hidden tkinter root ──
        ctk.set_appearance_mode("light")
        self.root = ctk.CTk()
        self.root.withdraw()

        # ── tray icon (background thread) ──
        d = self.fetcher.data
        sl = (100 - d["session_used_pct"]) / 100
        wl = (100 - d["weekly_used_pct"]) / 100

        menu = Menu(
            MenuItem('Open CodexBar', self._tray_open, default=True),
            MenuItem('Refresh', self._tray_refresh),
            MenuItem('Start with Windows', self._toggle_startup,
                     checked=lambda _item: startup_enabled()),
            Menu.SEPARATOR,
            MenuItem('Quit', self._tray_quit),
        )
        self.tray = pystray.Icon('CodexBar', make_icon(sl, wl, provider=self.active_provider),
                                 self._tooltip_text(), menu)
        threading.Thread(target=self.tray.run, daemon=True).start()

        # ── auto-refresh every 5 min ──
        self.root.after(300_000, self._auto_refresh)
        self.root.after(60_000, self._auto_quota_refresh)
        self.root.after(15_000, self._check_reset_warnings)

        print("\n" + "=" * 50)
        print("  CodexBar running in system tray!")
        print("  Look for the icon near the clock.")
        print("  Click ^ (arrow) if hidden.")
        print("  Double-click = open panel.")
        print("=" * 50 + "\n")

        # ── mainloop (blocks) ──
        self.root.mainloop()

    # ── tray callbacks (called from pystray thread) ──

    def _tray_open(self, *_):
        self.root.after(0, self._show_popup)

    def _tray_refresh(self, *_):
        self.root.after(0, self._do_refresh)

    def _toggle_startup(self, *_):
        set_startup(not startup_enabled())
        try:
            self.tray.update_menu()
        except Exception:
            pass

    def _tray_quit(self, *_):
        self.root.after(0, self._do_quit)

    # ── popup ──

    def _show_popup(self):
        if self.popup is not None:
            try:
                self.popup.destroy()
            except Exception:
                pass
            self.popup = None

        self.popup = CodexBarPopup(
            self.root,
            self.fetcher.data,
            codex_data=self.codex_data,
            on_close=self._on_popup_closed,
            on_refresh=lambda: self.root.after(0, self._do_refresh),
            on_quit=lambda: self.root.after(0, self._do_quit),
            on_tab_switch=self._on_tab_switch,
            initial_tab=self.active_provider,
            quota_history=self.quota_history,
            claude_quota_history=self.claude_history,
        )

    def _on_popup_closed(self):
        self.popup = None

    def _on_tab_switch(self, tab):
        self.active_provider = "openai" if tab == "openai" else "claude"
        self._set_tray_icon(self.active_provider)

    def _tooltip_text(self):
        """Tray tooltip: what is left on both windows of the provider on screen."""
        if self.active_provider == "openai":
            d, name = self.codex_data, "Codex"
            known = (d or {}).get("source") in ("api", "sessions")
        else:
            d, name = self.fetcher.data, "Claude"
            known = (d or {}).get("source") in ("cli", "api")
        parts = [f"{label} {100 - d[key]}% left"
                 for label, key in (("5h", "session_used_pct"), ("weekly", "weekly_used_pct"))
                 if known and (d or {}).get(key) is not None]
        return f"CodexBar — {name}: " + " · ".join(parts) if parts else f"CodexBar — {name}"

    def _set_tray_icon(self, provider):
        try:
            p = "openai" if provider == "openai" else "claude"
            self.tray.icon = make_icon(provider=p)
            self.tray.title = self._tooltip_text()
        except Exception:
            pass

    # ── refresh ──

    ALERT_AT = (80, 95)  # % of a quota window used

    def _alert_crossings(self, before, after, provider):
        """Notify each window that crossed an alert threshold since `before`.

        Comparing against the previous value keeps it quiet on repeat refreshes:
        a reset drops the percentage, which re-arms the next crossing.
        """
        for label, key in (("5h", "session_used_pct"), ("weekly", "weekly_used_pct")):
            prev = (before or {}).get(key, 0)
            now = (after or {}).get(key, 0)
            crossed = [t for t in self.ALERT_AT if prev < t <= now]
            if crossed:
                print(f"[CodexBar] Alert: {provider} {label} at {now}% (threshold {max(crossed)}%)")
                try:
                    self.tray.notify(f"{provider} {label} limit: {now}% used", "CodexBar")
                except Exception:
                    pass

    def _check_reset_warnings(self):
        if not self.running:
            return
        try:
            now = time.time()
            msgs = []
            for provider, d in (("Claude", self.fetcher.data), ("Codex", self.codex_data or {})):
                for label, pct, reset_key in (("5h", "session_used_pct", "session_reset_at"),
                                              ("weekly", "weekly_used_pct", "weekly_reset_at")):
                    used = d.get(pct) or 0
                    reset_at = d.get(reset_key)
                    if not _reset_warning_due(used, reset_at, now):
                        continue
                    key = (provider, label)
                    # shortcut: Claude usage is cached up to 5 min, so a crossing in that gap can be missed; refresh at T-1min to upgrade
                    if key in self._reset_warned and abs(self._reset_warned[key] - reset_at) < 120:
                        continue
                    self._reset_warned[key] = reset_at
                    print(f"[CodexBar] Reset soon: {provider} {label} at {used}% used")
                    msgs.append(f"{provider} {label}: {used}% used, resets in 1 min")
            if self.window_alerts:
                for provider, d in (("Claude", self.fetcher.data), ("Codex", self.codex_data or {})):
                    cur = d.get("session_reset_at")
                    used = d.get("session_used_pct") or 0
                    # stored value survives a quota refresh that already moved reset_at to the next window
                    for reset_at in {cur, self._session_reset_seen.get(provider)} - {None}:
                        event = _window_event(reset_at, now)
                        key = (provider, event)
                        if event is None or abs(self._window_warned.get(key, 0) - reset_at) < 120:
                            continue
                        self._window_warned[key] = reset_at
                        if event == "ending":
                            msgs.append(f"{provider} 5h window ends in {math.ceil((reset_at - now) / 60)} min ({100 - used}% left)")
                        else:
                            msgs.append(f"{provider} 5h window reset: quota available again")
                    # saved only after the candidates above, so the previous window's start is not lost
                    if cur and cur > now:
                        self._session_reset_seen[provider] = cur
            if msgs:
                self.tray.notify("\n".join(msgs), "CodexBar")
        except Exception as e:
            print(f"[CodexBar] Reset check err: {e}")
        finally:
            self.root.after(15_000, self._check_reset_warnings)

    def _do_refresh(self):
        def bg():
            claude_before = dict(self.fetcher.data)  # fetch_all mutates the same dict
            codex_before = self.codex_data
            self.fetcher.fetch_all()
            _record_quota_sample(self.claude_history, self.fetcher.data)
            try:
                self.codex_data = self.codex_fetcher.fetch()
            except Exception:
                pass
            d = self.fetcher.data
            if self.codex_data and self.codex_data.get("source") == "api":
                self.root.after(0, self._apply_codex_quota, dict(self.codex_data))
            # keep the icon of the provider the user is currently on
            # (a refresh used to silently flip it back to Claude)
            self.tray.icon = make_icon(
                (100 - d["session_used_pct"]) / 100,
                (100 - d["weekly_used_pct"]) / 100,
                provider=self.active_provider)
            self.tray.title = self._tooltip_text()
            print("[CodexBar] Refreshed")
            self._alert_crossings(claude_before, d, "Claude")
            self._alert_crossings(codex_before, self.codex_data, "Codex")
        threading.Thread(target=bg, daemon=True).start()

    def _auto_refresh(self):
        if not self.running:
            return
        self._do_refresh()
        self.root.after(300_000, self._auto_refresh)
    def _auto_quota_refresh(self):
        if not self.running:
            return

        def bg():
            try:
                data = self.codex_fetcher.fetch_quota()
            except Exception:
                data = None
            if data and self.running and self.root:
                try:
                    self.root.after(0, self._apply_codex_quota, data)
                except Exception:
                    pass

        threading.Thread(target=bg, daemon=True).start()
        self.root.after(60_000, self._auto_quota_refresh)

    def _apply_codex_quota(self, data):
        if not data or data.get("source") != "api":
            return
        _record_quota_sample(self.quota_history, data)
        fields = ("session_used_pct", "session_reset", "session_reset_at",
                  "session_observed_at", "session_window_seconds", "weekly_used_pct",
                  "weekly_reset", "weekly_reset_at", "weekly_observed_at",
                  "weekly_window_seconds", "source", "updated", "available", "plan")
        if self.codex_data is None:
            self.codex_data = CodexDataFetcher._empty()
        self.codex_data.update({key: data[key] for key in fields if key in data})
        if self.popup:
            self.popup.update_codex_quota(self.codex_data)
        try:
            self.tray.title = self._tooltip_text()
        except Exception:
            pass


    # ── quit ──

    def _do_quit(self):
        print("[CodexBar] Bye!")
        self.running = False
        try:
            self.tray.stop()
        except Exception:
            pass
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:
            pass
        sys.exit(0)


# Named mutex: a second copy finds it already created and exits.
_SINGLE_INSTANCE = "Local\\CodexBarWinSingleInstance"
_ERROR_ALREADY_EXISTS = 183


def _acquire_single_instance(name=_SINGLE_INSTANCE):
    """True when no other CodexBar holds the mutex; the handle lives until exit."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW(None, False, name)
    return ctypes.get_last_error() != _ERROR_ALREADY_EXISTS


def _check_single_instance():
    name = f"Local\\CodexBarWinTest{os.getpid()}"
    assert _acquire_single_instance(name)
    assert not _acquire_single_instance(name)


# ─────────────────────────────────────────────

if __name__ == '__main__':
    if '--check-forecast' in sys.argv:
        _check_quota_forecast()
        print('Quota forecast checks passed')
        raise SystemExit(0)
    if '--check-single-instance' in sys.argv:
        _check_single_instance()
        print('Single-instance check passed')
        raise SystemExit(0)
    if not _acquire_single_instance():
        # windowed exe has no console: the dialog is the only feedback
        print('[CodexBar] Already running.')
        ctypes.windll.user32.MessageBoxW(None, "CodexBar è già in esecuzione (icona vicino all'orologio).", "CodexBar", 0x40)
        raise SystemExit(0)
    print(r"""
   ========================================
    CodexBar for Windows v1.2.0
    Native popup — no browser needed
   ========================================
    """)
    CodexBarApp(prefer_codex='--codex' in sys.argv, window_alerts='--no-window-alerts' not in sys.argv).start()
