import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mods" / "codex-bar"))
import collector

NOW = 1_800_000_000


def stamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


class CollectorTests(unittest.TestCase):
    def test_live_response_parses_windows_without_defaulting_missing_values(self):
        result = collector.parse_live_usage({
            "plan_type": "plus",
            "rate_limit": {
                "primary_window": {"used_percent": 25, "reset_at": NOW + 3600},
                "secondary_window": {},
            },
        }, NOW)
        self.assertEqual(result["plan"], "plus")
        self.assertEqual(result["primary"], {"used_percent": 25, "reset_at": NOW + 3600})
        self.assertIsNone(result["secondary"])

    def test_snapshot_supports_both_rollout_shapes_and_ignores_malformed_lines(self):
        limits = {"primary": {"used_percent": 40, "resets_at": NOW + 1000}}
        lines = ["[]\n", "not-json\n", json.dumps({
            "type": "event_msg", "timestamp": stamp(NOW),
            "payload": {"type": "token_count", "rate_limits": limits},
        }), json.dumps({
            "type": "event_msg", "timestamp": stamp(NOW + 1),
            "payload": {"type": "token_count", "info": {"rate_limits": limits}},
        })]
        result = collector.parse_snapshot_lines(lines, NOW + 2)
        self.assertEqual(result["primary"]["used_percent"], 40)
        self.assertEqual(result["observed_at"], NOW + 1)

    def test_expired_snapshot_is_unknown_not_zero(self):
        result = collector.parse_snapshot_lines([json.dumps({
            "type": "event_msg", "timestamp": stamp(NOW - 2000),
            "payload": {"type": "token_count", "rate_limits": {
                "primary": {"used_percent": 70, "resets_at": NOW - 1},
            }},
        })], NOW)
        self.assertIsNone(result["primary"]["used_percent"])
        self.assertTrue(result["primary"]["expired"])
        self.assertTrue(result["stale"])

    def test_context_matches_focused_cwd_and_uses_latest_prompt_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp)
            rollout = sessions / "rollout-a.jsonl"
            rollout.write_text("\n".join([
                json.dumps({"type": "session_meta", "payload": {"cwd": "/work/project"}}),
                json.dumps({"type": "turn_context", "timestamp": stamp(NOW - 1), "payload": {"model": "gpt-6-codex"}}),
                json.dumps({"type": "event_msg", "timestamp": stamp(NOW), "payload": {
                    "type": "token_count", "info": {
                        "model_context_window": 500,
                        "last_token_usage": {"input_tokens": 200, "output_tokens": 20},
                    },
                }}),
            ]), encoding="utf-8")
            os.utime(rollout, (NOW, NOW))
            result = collector.context_for_cwd("/work/project/", sessions, NOW)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["model"], "gpt-6-codex")
        self.assertEqual(result["context_percent"], 40)

    def test_context_is_unavailable_for_ambiguous_or_stale_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp)
            content = json.dumps({"type": "session_meta", "payload": {"cwd": "/work/project"}}) + "\n"
            for name in ("a", "b"):
                path = sessions / f"rollout-{name}.jsonl"
                path.write_text(content, encoding="utf-8")
                os.utime(path, (NOW, NOW))
            ambiguous = collector.context_for_cwd("/work/project", sessions, NOW)
            self.assertEqual(ambiguous["reason"], "ambiguous")
            for path in sessions.glob("*.jsonl"):
                os.utime(path, (NOW - collector.CONTEXT_MAX_AGE - 1, NOW - collector.CONTEXT_MAX_AGE - 1))
            stale = collector.context_for_cwd("/work/project", sessions, NOW)
            self.assertEqual(stale["reason"], "no_recent_match")

    def test_live_fetch_sends_auth_only_to_usage_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth = Path(tmp) / "auth.json"
            auth.write_text(json.dumps({"tokens": {"access_token": "fixture-token", "account_id": "fixture-account"}}), encoding="utf-8")
            calls = []

            def opener(request, timeout):
                calls.append((request, timeout))
                return FakeResponse({"plan_type": "pro", "rate_limit": {
                    "primary_window": {"used_percent": 10, "reset_at": NOW + 50},
                }})

            result, error = collector.fetch_live_usage(auth, opener, NOW)
        request, timeout = calls[0]
        self.assertIsNone(error)
        self.assertEqual(result["source"], "api")
        self.assertEqual(request.full_url, collector.USAGE_URL)
        self.assertEqual(request.get_header("Authorization"), "Bearer fixture-token")
        self.assertEqual(request.get_header("Chatgpt-account-id"), "fixture-account")
        self.assertEqual(timeout, 8)


    def test_claude_usage_maps_oauth_windows_and_sends_token_only_to_usage_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            creds = Path(tmp) / "credentials.json"
            creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "sk-test", "subscriptionType": "pro"}}), encoding="utf-8")
            seen = {}

            def opener(request, timeout):
                seen["url"] = request.full_url
                seen["auth"] = request.get_header("Authorization")
                return FakeResponse({
                    "five_hour": {"utilization": 13.4, "resets_at": stamp(NOW + 3600)},
                    "seven_day": {"utilization": 41, "resets_at": stamp(NOW + 86400)},
                })

            quota = collector.fetch_claude_usage(creds, opener=opener, now=NOW, cache_path=Path(tmp) / "quota-cache.json")

        self.assertEqual(seen["url"], collector.CLAUDE_USAGE_URL)
        self.assertEqual(seen["auth"], "Bearer sk-test")
        self.assertEqual(quota["source"], "api")
        self.assertEqual(quota["plan"], "Pro")
        self.assertEqual(quota["primary"], {"used_percent": 13, "reset_at": NOW + 3600})
        self.assertEqual(quota["secondary"], {"used_percent": 41, "reset_at": NOW + 86400})

if __name__ == "__main__":
    unittest.main()
