import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import anti_estimator
import star_tokens


class DashboardUsageTests(unittest.TestCase):
    def test_build_api_response_keeps_codex_and_claude_separate(self):
        codex = {
            "daily": [
                {
                    "date": "2026-05-13",
                    "totals": {
                        "input_tokens": 10,
                        "cache_read_input_tokens": 20,
                        "output_tokens": 30,
                        "reasoning_output_tokens": 4,
                        "total_tokens": 64,
                        "cost_usd": 1.25,
                    },
                    "models": {"gpt-5.5": {"total_tokens": 64}},
                }
            ]
        }
        claude = {
            "daily": [
                {
                    "date": "2026-05-13",
                    "totals": {
                        "input_tokens": 2,
                        "cache_creation_input_tokens": 3,
                        "cache_read_input_tokens": 5,
                        "output_tokens": 7,
                        "reasoning_output_tokens": 0,
                        "total_tokens": 17,
                        "cost_usd": 0.75,
                    },
                    "models": {"claude-opus-4-7": {"total_tokens": 17}},
                }
            ]
        }

        with (
            mock.patch.object(star_tokens, "get_codex_data", return_value=codex),
            mock.patch.object(star_tokens, "get_claude_data", return_value=claude, create=True),
            mock.patch.object(star_tokens, "get_anti_data", return_value={}),
            mock.patch.object(star_tokens, "get_anti_status", return_value={"running": False, "pid": None}),
            mock.patch.object(star_tokens, "get_claude_validation_summary", return_value=None, create=True),
        ):
            response = star_tokens.build_api_response()

        entry = response["daily"][0]
        self.assertEqual(entry["codex"]["total_tokens"], 64)
        self.assertEqual(entry["codex"]["models"], ["gpt-5.5"])
        self.assertEqual(entry["claude"]["total_tokens"], 17)
        self.assertEqual(entry["claude"]["cache_creation_tokens"], 3)
        self.assertIsNone(entry["antigravity"])

    def test_scan_claude_usage_files_dedupes_message_request_pairs(self):
        with tempfile.TemporaryDirectory() as tmp:
            usage_file = Path(tmp) / "session.jsonl"
            duplicate = {
                "type": "assistant",
                "timestamp": "2026-05-13T01:00:00Z",
                "requestId": "req-1",
                "message": {
                    "id": "msg-1",
                    "model": "claude-opus-4-7",
                    "usage": {
                        "input_tokens": 2,
                        "cache_creation_input_tokens": 3,
                        "cache_read_input_tokens": 5,
                        "output_tokens": 7,
                    },
                },
            }
            second = {
                "type": "assistant",
                "timestamp": "2026-05-13T02:00:00Z",
                "requestId": "req-2",
                "message": {
                    "id": "msg-2",
                    "model": "claude-haiku-4-5-20251001",
                    "usage": {
                        "input_tokens": 11,
                        "cache_creation_input_tokens": 13,
                        "cache_read_input_tokens": 17,
                        "output_tokens": 19,
                    },
                },
            }
            usage_file.write_text(
                "\n".join(json.dumps(item) for item in [duplicate, duplicate, second]),
                encoding="utf-8",
            )

            summary = star_tokens.scan_claude_usage_files([usage_file])

        day = summary["daily"][0]
        totals = day["totals"]
        self.assertEqual(totals["input_tokens"], 13)
        self.assertEqual(totals["cache_creation_input_tokens"], 16)
        self.assertEqual(totals["cache_read_input_tokens"], 22)
        self.assertEqual(totals["output_tokens"], 26)
        self.assertEqual(totals["total_tokens"], 77)
        self.assertGreater(totals["cost_usd"], 0)
        self.assertEqual(summary["stats"]["deduped_entries"], 1)

    def test_claude_data_prefers_local_parser_when_tu_mismatch_is_large(self):
        tu = {"daily": [{"date": "2026-05-13", "totals": {"total_tokens": 200, "cost_usd": 2.0}, "models": {}}]}
        local = {
            "daily": [{"date": "2026-05-13", "totals": {"total_tokens": 100, "cost_usd": 1.0}, "models": {}}],
            "totals": {"total_tokens": 100, "cost_usd": 1.0},
            "source": "local_jsonl_dedup",
        }

        with mock.patch.object(star_tokens, "get_claude_local_data", return_value=local):
            result = star_tokens.get_claude_data(tu)

        self.assertEqual(result["source"], "local_jsonl_dedup")
        self.assertEqual(result["tu_total_tokens"], 200)
        self.assertEqual(result["tu_delta_pct"], -50.0)

    def test_antigravity_primary_prefers_gemini_low_over_thinking_old_usage(self):
        models = [
            {"label": "Claude Opus 4.6 (Thinking)", "remaining_fraction": 0.6, "reset_time": "2026-05-13T06:54:51Z"},
            {"label": "Gemini 3.1 Pro (Low)", "remaining_fraction": 1.0, "reset_time": "2026-05-13T11:07:04Z"},
            {"label": "Gemini 3 Flash", "remaining_fraction": 1.0, "reset_time": "2026-05-13T11:07:04Z"},
        ]

        primary = anti_estimator.select_primary_quota_model(models)

        self.assertEqual(primary["label"], "Gemini 3.1 Pro (Low)")
        self.assertEqual(anti_estimator.used_pct_from_quota_model(primary), 0.0)

    def test_launch_native_window_missing_pywebview_does_not_open_browser(self):
        real_import = __import__

        def fake_import(name, *args, **kwargs):
            if name == "webview":
                raise ModuleNotFoundError("No module named 'webview'")
            return real_import(name, *args, **kwargs)

        with (
            mock.patch("builtins.__import__", side_effect=fake_import),
            mock.patch.object(star_tokens.webbrowser, "open") as open_browser,
        ):
            launched = star_tokens.launch_native_window("http://127.0.0.1:18877")

        self.assertFalse(launched)
        open_browser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
