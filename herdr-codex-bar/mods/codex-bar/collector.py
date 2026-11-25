#!/usr/bin/env python3
"""Small, stdlib-only Codex quota and focused-session collector for Herdr."""
import base64
import binascii
import collections
import json
import math
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_CACHE = Path(__file__).with_name("quota-cache.json")
CACHE_TTL = 300
CONTEXT_MAX_AGE = 30 * 60
MAX_ROLLOUTS = 25
MAX_TAIL_LINES = 2000


def _number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _epoch(value):
    number = _number(value)
    return int(number) if number is not None and number > 0 else None


def _window(raw, reset_key):
    if not isinstance(raw, dict):
        return None
    used = _number(raw.get("used_percent"))
    reset = _epoch(raw.get(reset_key))
    if used is None and reset is None:
        return None
    return {
        "used_percent": int(round(max(0, min(100, used)))) if used is not None else None,
        "reset_at": reset,
    }


def parse_live_usage(payload, now=None):
    if not isinstance(payload, dict):
        return None
    rate = payload.get("rate_limit") or {}
    if not isinstance(rate, dict):
        rate = {}
    return {
        "source": "api",
        "observed_at": int(time.time() if now is None else now),
        "plan": payload.get("plan_type") if isinstance(payload.get("plan_type"), str) else None,
        "primary": _window(rate.get("primary_window"), "reset_at"),
        "secondary": _window(rate.get("secondary_window"), "reset_at"),
        "error": None,
    }


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except (ValueError, OverflowError):
        return None


def parse_snapshot_lines(lines, now=None):
    now = time.time() if now is None else now
    latest = None
    for line in lines:
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        if event.get("type") != "event_msg" or payload.get("type") != "token_count":
            continue
        info = payload.get("info") or {}
        limits = payload.get("rate_limits") or (info.get("rate_limits") if isinstance(info, dict) else None)
        if isinstance(limits, dict):
            latest = (limits, _timestamp(event.get("timestamp")))
    if latest is None:
        return None
    limits, observed = latest
    primary = _window(limits.get("primary"), "resets_at")
    secondary = _window(limits.get("secondary"), "resets_at")
    for window in (primary, secondary):
        if window and window["reset_at"] is not None and window["reset_at"] <= now:
            window["used_percent"] = None
            window["expired"] = True
    if not primary and not secondary:
        return None
    return {
        "source": "snapshot",
        "observed_at": observed,
        "stale": observed is None or now - observed > CONTEXT_MAX_AGE,
        "plan": limits.get("plan_type") if isinstance(limits.get("plan_type"), str) else None,
        "primary": primary,
        "secondary": secondary,
        "error": None,
    }


def _claims(access_token):
    try:
        payload = access_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return claims.get("https://api.openai.com/auth", {}) if isinstance(claims, dict) else {}
    except (IndexError, ValueError, TypeError, binascii.Error):
        return {}


def fetch_live_usage(auth_path=None, opener=urlopen, now=None):
    auth_path = Path(auth_path) if auth_path else Path.home() / ".codex" / "auth.json"
    try:
        auth = json.loads(auth_path.read_text(encoding="utf-8"))
        if not isinstance(auth, dict):
            return None, "auth_unavailable"
        tokens = auth.get("tokens") or {}
        if not isinstance(tokens, dict):
            return None, "auth_unavailable"
        access = tokens.get("access_token")
        if not isinstance(access, str) or not access:
            return None, "auth_unavailable"
        account = tokens.get("account_id") or _claims(access).get("chatgpt_account_id", "")
        if not isinstance(account, str):
            account = ""
        request = Request(USAGE_URL, headers={
            "Authorization": f"Bearer {access}",
            "chatgpt-account-id": account,
            "User-Agent": "codex-cli",
            "Accept": "application/json",
        })
        with opener(request, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return parse_live_usage(payload, now), None
    except HTTPError:
        return None, "http_error"
    except (OSError, URLError, ValueError, TypeError, KeyError):
        return None, "api_unavailable"


def _read_meta(path):
    try:
        with path.open(encoding="utf-8", errors="ignore") as stream:
            for _ in range(20):
                line = stream.readline()
                if not line:
                    break
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                payload = event.get("payload") or {}
                if not isinstance(payload, dict):
                    continue
                if event.get("type") == "session_meta":
                    return payload
    except OSError:
        pass
    return {}


def _norm_cwd(cwd):
    return str(cwd).rstrip("/") or "/" if cwd else ""


def context_for_cwd(cwd, sessions_dir=None, now=None):
    now = time.time() if now is None else now
    if not isinstance(cwd, str) or not cwd:
        return {"status": "unavailable", "reason": "no_cwd"}
    sessions_dir = Path(sessions_dir) if sessions_dir else Path.home() / ".codex" / "sessions"
    target = _norm_cwd(cwd)
    try:
        rollouts = sorted(sessions_dir.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return {"status": "unavailable", "reason": "no_sessions"}
    # shortcut: scan only the newest 25 plain-JSONL rollouts; raise the cap if context lookup misses a live session.
    matches = []
    for path in rollouts[:MAX_ROLLOUTS]:
        try:
            age = now - path.stat().st_mtime
        except OSError:
            continue
        if age < 0 or age > CONTEXT_MAX_AGE:
            continue
        meta = _read_meta(path)
        if _norm_cwd(meta.get("cwd")) == target:
            matches.append(path)
    if len(matches) != 1:
        return {"status": "unavailable", "reason": "ambiguous" if matches else "no_recent_match"}
    path = matches[0]
    try:
        with path.open(encoding="utf-8", errors="ignore") as stream:
            tail = collections.deque(stream, maxlen=MAX_TAIL_LINES)
    except OSError:
        return {"status": "unavailable", "reason": "unreadable"}
    model = None
    info = None
    model_at = None
    observed = None
    for line in reversed(tail):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        if event.get("type") == "turn_context" and model is None:
            model = payload.get("model")
            model_at = _timestamp(event.get("timestamp"))
        if event.get("type") == "event_msg" and payload.get("type") == "token_count" and info is None:
            info = payload.get("info") or {}
            if not isinstance(info, dict):
                info = {}
            observed = _timestamp(event.get("timestamp"))
        if model and info is not None:
            break
    window = _number((info or {}).get("model_context_window"))
    usage = (info or {}).get("last_token_usage") or {}
    input_tokens = _number(usage.get("input_tokens")) if isinstance(usage, dict) else None
    if not isinstance(model, str) or not model or not window or input_tokens is None or observed is None or model_at is None or model_at > observed or observed - model_at > CONTEXT_MAX_AGE or now - observed > CONTEXT_MAX_AGE:
        return {"status": "unavailable", "reason": "stale_or_incomplete"}
    return {
        "status": "available",
        "model": model,
        "context_tokens": int(input_tokens),
        "context_window": int(window),
        "context_percent": max(0, min(100, round(input_tokens * 100 / window))),
        "observed_at": observed,
    }


def collect_quota(auth_path=None, sessions_dir=None, opener=urlopen, now=None):
    live, error = fetch_live_usage(auth_path, opener, now)
    if live and (live["primary"] or live["secondary"]):
        return live
    sessions_dir = Path(sessions_dir) if sessions_dir else Path.home() / ".codex" / "sessions"
    try:
        rollouts = sorted(sessions_dir.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        rollouts = []
    for path in rollouts[:5]:
        try:
            with path.open(encoding="utf-8", errors="ignore") as stream:
                snapshot = parse_snapshot_lines(stream, now)
        except OSError:
            continue
        if snapshot:
            snapshot["error"] = error
            return snapshot
    return {
        "source": "unavailable",
        "observed_at": None,
        "plan": None,
        "primary": None,
        "secondary": None,
        "error": error or "quota_unavailable",
    }


def _claude_window(raw):
    if not isinstance(raw, dict):
        return None
    used = _number(raw.get("utilization"))
    reset = _timestamp(raw.get("resets_at"))
    if used is None and reset is None:
        return None
    return {
        "used_percent": int(round(max(0, min(100, used)))) if used is not None else None,
        "reset_at": reset,
    }


def _read_cache(path):
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return cached if isinstance(cached, dict) else {}


def _write_cache(path, state):
    try:
        path.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


def _retry_seconds(headers):
    try:
        return max(0, int((headers or {}).get("Retry-After") or 0))
    except (TypeError, ValueError):
        return 0


def fetch_claude_usage(creds_path=None, opener=urlopen, now=None, cache_path=None):
    """Claude 5h and weekly quota, using the OAuth token Claude Code keeps locally.

    The API is tried at most once per CACHE_TTL, failures included, so reloads never hit it.
    On failure the last good value is shown with its real age.
    """
    creds_path = Path(creds_path) if creds_path else Path.home() / ".claude" / ".credentials.json"
    cache_path = Path(cache_path) if cache_path else CLAUDE_CACHE
    observed = int(time.time() if now is None else now)
    state = _read_cache(cache_path)
    unavailable = {"source": "unavailable", "observed_at": None, "plan": None, "primary": None, "secondary": None}
    fallback = state if state.get("source") == "api" else unavailable
    if observed < state.get("next_attempt", 0):
        return {**fallback, "error": state.get("error")}
    try:
        oauth = json.loads(creds_path.read_text(encoding="utf-8")).get("claudeAiOauth") or {}
        token = oauth.get("accessToken")
        if not isinstance(token, str) or not token:
            return {**fallback, "error": "auth_unavailable"}
        request = Request(CLAUDE_USAGE_URL, headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        })
        with opener(request, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
        result = {
            "source": "api",
            "observed_at": observed,
            "plan": str(oauth.get("subscriptionType") or "").capitalize() or None,
            "primary": _claude_window(payload.get("five_hour")),
            "secondary": _claude_window(payload.get("seven_day")),
            "error": None,
        }
        _write_cache(cache_path, {**result, "next_attempt": observed + CACHE_TTL})
        return result
    except HTTPError as e:
        error, wait = "http_error", max(CACHE_TTL, _retry_seconds(e.headers))
    except (OSError, URLError, ValueError, TypeError, KeyError, AttributeError):
        error, wait = "api_unavailable", CACHE_TTL
    _write_cache(cache_path, {**state, "next_attempt": observed + wait, "error": error})
    return {**fallback, "error": error}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        result = {"error": "missing_mode"}
    elif argv[0] == "quota":
        result = fetch_claude_usage()
    elif argv[0] == "context":
        result = context_for_cwd(argv[1] if len(argv) > 1 else "")
    else:
        result = {"error": "invalid_mode"}
    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
