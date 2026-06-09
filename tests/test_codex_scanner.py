import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

from token_dashboard.codex_scanner import (
    parse_codex_model_usage,
    parse_codex_session,
    scan_codex_dir,
)
from token_dashboard.db import init_db


FIXTURE = Path(__file__).parent / "fixtures" / "codex_session.jsonl"


def _token_event(timestamp, input_tokens, cached_input_tokens, output_tokens, reasoning):
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": cached_input_tokens,
                    "output_tokens": output_tokens,
                    "reasoning_output_tokens": reasoning,
                }
            },
        },
    }


def _turn_context(timestamp, model):
    return {
        "timestamp": timestamp,
        "type": "turn_context",
        "payload": {"model": model},
    }


def _write_complete_lines(path, records):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for record in records:
            if isinstance(record, str):
                f.write(record + "\n")
            else:
                f.write(json.dumps(record) + "\n")


class ParseCodexSessionTests(unittest.TestCase):
    def test_parse_uses_final_cumulative_token_event(self):
        row = parse_codex_session(FIXTURE)

        self.assertEqual(row["session_id"], "codex-session-1")
        self.assertEqual(row["day"], "2026-06-06")
        self.assertEqual(row["input_tokens"], 250)
        self.assertEqual(row["cached_input_tokens"], 100)
        self.assertEqual(row["output_tokens"], 25)
        self.assertEqual(row["reasoning_output_tokens"], 5)

    def test_parse_ignores_malformed_and_partial_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fallback-session.jsonl"
            records = [
                {"timestamp": "2026-06-06T07:00:00Z", "type": "session_meta",
                 "payload": {"id": "metadata-session"}},
                "{malformed",
                _token_event("2026-06-06T07:01:00Z", 50, 20, 7, 3),
            ]
            _write_complete_lines(path, records)
            with open(path, "ab") as f:
                f.write(json.dumps(
                    _token_event("2026-06-06T07:02:00Z", 999, 500, 99, 9)
                ).encode("utf-8"))

            row = parse_codex_session(path)

        self.assertEqual(row["session_id"], "metadata-session")
        self.assertEqual(row["input_tokens"], 50)
        self.assertEqual(row["output_tokens"], 7)

    def test_parse_uses_filename_stem_without_session_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stem-session.jsonl"
            _write_complete_lines(
                path, [_token_event("2026-06-06T07:01:00Z", 8, 3, 2, 1)]
            )

            row = parse_codex_session(path)

        self.assertEqual(row["session_id"], "stem-session")

    def test_parse_converts_final_timestamp_to_machine_local_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            timestamp = "2026-06-06T23:30:00Z"
            path = Path(tmp) / "session.jsonl"
            _write_complete_lines(path, [_token_event(timestamp, 1, 0, 0, 0)])

            row = parse_codex_session(path)

        expected = datetime.fromisoformat(
            timestamp.replace("Z", "+00:00")
        ).astimezone().date().isoformat()
        self.assertEqual(row["day"], expected)

    def test_parse_attributes_cumulative_deltas_to_active_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            _write_complete_lines(path, [
                _turn_context("2026-06-06T07:00:00Z", "model-a"),
                _token_event("2026-06-06T07:01:00Z", 100, 40, 10, 2),
                _turn_context("2026-06-06T07:02:00Z", "model-b"),
                _token_event("2026-06-06T07:03:00Z", 250, 100, 25, 5),
            ])
            rows = parse_codex_model_usage(path)
        self.assertEqual(rows, [
            {
                "model": "model-a", "day": "2026-06-06",
                "input_tokens": 100, "cached_input_tokens": 40,
                "output_tokens": 10, "reasoning_output_tokens": 2,
            },
            {
                "model": "model-b", "day": "2026-06-06",
                "input_tokens": 150, "cached_input_tokens": 60,
                "output_tokens": 15, "reasoning_output_tokens": 3,
            },
        ])

    def test_parse_splits_deltas_across_local_days(self):
        first = "2026-06-06T15:00:00Z"
        second = "2026-06-06T20:30:00Z"
        if datetime.fromisoformat(first.replace("Z", "+00:00")).astimezone().date() == \
                datetime.fromisoformat(second.replace("Z", "+00:00")).astimezone().date():
            self.skipTest("timestamps do not cross a local-day boundary here")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            _write_complete_lines(path, [
                _turn_context(first, "model-a"),
                _token_event(first, 10, 2, 1, 0),
                _token_event(second, 25, 5, 3, 1),
            ])
            rows = parse_codex_model_usage(path)
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["day"], rows[1]["day"])

    def test_parse_clamps_negative_reset_deltas_to_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            _write_complete_lines(path, [
                _turn_context("2026-06-06T07:00:00Z", "model-a"),
                _token_event("2026-06-06T07:01:00Z", 100, 40, 10, 2),
                _token_event("2026-06-06T07:02:00Z", 10, 4, 1, 0),
            ])
            rows = parse_codex_model_usage(path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["input_tokens"], 100)

    def test_parse_uses_unknown_codex_before_first_model_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            _write_complete_lines(path, [
                _token_event("2026-06-06T07:01:00Z", 10, 2, 1, 0),
            ])
            rows = parse_codex_model_usage(path)
        self.assertEqual(rows[0]["model"], "unknown-codex")


class ScanCodexDirTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "usage.db")
        self.root = os.path.join(self.tmp, "sessions")
        os.makedirs(os.path.join(self.root, "nested"))
        self.session = os.path.join(self.root, "nested", "session.jsonl")
        shutil.copy(FIXTURE, self.session)
        init_db(self.db)

    def test_scan_recurses_and_skips_unchanged_files(self):
        first = scan_codex_dir(self.root, self.db)
        second = scan_codex_dir(self.root, self.db)

        self.assertEqual(first, {"files": 1, "sessions": 1})
        self.assertEqual(second, {"files": 0, "sessions": 0})

    def test_scan_skips_unchanged_partial_eof_without_losing_valid_snapshot(self):
        with open(self.session, "ab") as f:
            f.write(b'{"timestamp":"2026-06-06T07:03:00Z","type":"event_msg"')

        first = scan_codex_dir(self.root, self.db)
        second = scan_codex_dir(self.root, self.db)

        self.assertEqual(first, {"files": 1, "sessions": 1})
        self.assertEqual(second, {"files": 0, "sessions": 0})
        with sqlite3.connect(self.db) as c:
            row = c.execute(
                """
                SELECT input_tokens, output_tokens, bytes_read
                  FROM provider_sessions
                 WHERE provider='codex' AND session_id='codex-session-1'
                """
            ).fetchone()
        self.assertEqual(row, (250, 25, os.path.getsize(self.session)))

    def test_scan_replaces_changed_session_without_duplication(self):
        scan_codex_dir(self.root, self.db)
        with open(self.session, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(
                _token_event("2026-06-06T07:03:00Z", 400, 150, 40, 8)
            ) + "\n")
        future = time.time() + 10
        os.utime(self.session, (future, future))

        result = scan_codex_dir(self.root, self.db)

        self.assertEqual(result, {"files": 1, "sessions": 1})
        with sqlite3.connect(self.db) as c:
            rows = c.execute(
                "SELECT session_id, input_tokens, output_tokens "
                "FROM provider_sessions WHERE provider='codex'"
            ).fetchall()
        self.assertEqual(rows, [("codex-session-1", 400, 40)])

    def test_scan_stores_and_replaces_model_contributions(self):
        _write_complete_lines(self.session, [
            {"type": "session_meta", "payload": {"id": "codex-session-1"}},
            _turn_context("2026-06-06T07:00:00Z", "model-a"),
            _token_event("2026-06-06T07:01:00Z", 100, 40, 10, 2),
            _turn_context("2026-06-06T07:02:00Z", "model-b"),
            _token_event("2026-06-06T07:03:00Z", 250, 100, 25, 5),
        ])
        scan_codex_dir(self.root, self.db)
        with sqlite3.connect(self.db) as c:
            rows = c.execute("""
              SELECT model, input_tokens FROM codex_model_usage
               ORDER BY model
            """).fetchall()
        self.assertEqual(rows, [("model-a", 100), ("model-b", 150)])

        _write_complete_lines(self.session, [
            {"type": "session_meta", "payload": {"id": "codex-session-1"}},
            _turn_context("2026-06-06T07:00:00Z", "model-c"),
            _token_event("2026-06-06T07:01:00Z", 50, 10, 5, 1),
        ])
        future = time.time() + 10
        os.utime(self.session, (future, future))
        scan_codex_dir(self.root, self.db)
        with sqlite3.connect(self.db) as c:
            rows = c.execute("""
              SELECT model, input_tokens FROM codex_model_usage
               ORDER BY model
            """).fetchall()
        self.assertEqual(rows, [("model-c", 50)])

    def test_scan_backfills_missing_model_contributions_for_unchanged_session(self):
        scan_codex_dir(self.root, self.db)
        with sqlite3.connect(self.db) as c:
            c.execute("DELETE FROM codex_model_usage")
            c.commit()

        result = scan_codex_dir(self.root, self.db)

        self.assertEqual(result, {"files": 1, "sessions": 1})
        with sqlite3.connect(self.db) as c:
            count = c.execute(
                "SELECT COUNT(*) FROM codex_model_usage"
            ).fetchone()[0]
        self.assertGreater(count, 0)

    def test_scan_removes_session_when_source_file_is_deleted(self):
        scan_codex_dir(self.root, self.db)
        os.remove(self.session)

        result = scan_codex_dir(self.root, self.db)

        self.assertEqual(result, {"files": 0, "sessions": 0})
        with sqlite3.connect(self.db) as c:
            count = c.execute(
                "SELECT COUNT(*) FROM provider_sessions WHERE provider='codex'"
            ).fetchone()[0]
        self.assertEqual(count, 0)
        with sqlite3.connect(self.db) as c:
            usage_count = c.execute(
                "SELECT COUNT(*) FROM codex_model_usage"
            ).fetchone()[0]
        self.assertEqual(usage_count, 0)

    def test_duplicate_session_ids_choose_stable_path_and_skip_next_scan(self):
        winner = os.path.join(self.root, "a-winner.jsonl")
        _write_complete_lines(
            winner,
            [
                {"type": "session_meta", "payload": {"id": "codex-session-1"}},
                _token_event("2026-06-06T08:00:00Z", 600, 200, 60, 10),
            ],
        )

        first = scan_codex_dir(self.root, self.db)
        second = scan_codex_dir(self.root, self.db)

        self.assertEqual(first, {"files": 2, "sessions": 1})
        self.assertEqual(second, {"files": 0, "sessions": 0})
        with sqlite3.connect(self.db) as c:
            row = c.execute(
                """
                SELECT path, input_tokens, output_tokens
                  FROM provider_sessions
                 WHERE provider='codex' AND session_id='codex-session-1'
                """
            ).fetchone()
        self.assertEqual(row, (winner, 600, 60))

    def test_deleting_duplicate_winner_promotes_remaining_file(self):
        winner = os.path.join(self.root, "a-winner.jsonl")
        _write_complete_lines(
            winner,
            [
                {"type": "session_meta", "payload": {"id": "codex-session-1"}},
                _token_event("2026-06-06T08:00:00Z", 600, 200, 60, 10),
            ],
        )
        scan_codex_dir(self.root, self.db)
        os.remove(winner)

        result = scan_codex_dir(self.root, self.db)

        self.assertEqual(result, {"files": 1, "sessions": 1})
        with sqlite3.connect(self.db) as c:
            row = c.execute(
                """
                SELECT path, input_tokens, output_tokens
                  FROM provider_sessions
                 WHERE provider='codex' AND session_id='codex-session-1'
                """
            ).fetchone()
        self.assertEqual(row, (self.session, 250, 25))

    def test_invalidated_duplicate_winner_promotes_remaining_file(self):
        winner = os.path.join(self.root, "a-winner.jsonl")
        _write_complete_lines(
            winner,
            [
                {"type": "session_meta", "payload": {"id": "codex-session-1"}},
                _token_event("2026-06-06T08:00:00Z", 600, 200, 60, 10),
            ],
        )
        scan_codex_dir(self.root, self.db)
        _write_complete_lines(
            winner,
            [{"type": "session_meta", "payload": {"id": "codex-session-1"}}],
        )

        result = scan_codex_dir(self.root, self.db)

        self.assertEqual(result, {"files": 2, "sessions": 1})
        with sqlite3.connect(self.db) as c:
            row = c.execute(
                """
                SELECT path, input_tokens, output_tokens
                  FROM provider_sessions
                 WHERE provider='codex' AND session_id='codex-session-1'
                """
            ).fetchone()
        self.assertEqual(row, (self.session, 250, 25))

    def test_scan_skips_unchanged_file_without_token_snapshot(self):
        invalid = os.path.join(self.root, "invalid.jsonl")
        _write_complete_lines(
            invalid,
            [{"type": "session_meta", "payload": {"id": "no-usage"}}],
        )

        first = scan_codex_dir(self.root, self.db)
        second = scan_codex_dir(self.root, self.db)

        self.assertEqual(first, {"files": 2, "sessions": 1})
        self.assertEqual(second, {"files": 0, "sessions": 0})
        with sqlite3.connect(self.db) as c:
            count = c.execute(
                """
                SELECT COUNT(*) FROM provider_sessions
                 WHERE provider='codex' AND session_id='no-usage'
                """
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_missing_root_is_not_an_error(self):
        result = scan_codex_dir(os.path.join(self.tmp, "missing"), self.db)

        self.assertEqual(result, {"files": 0, "sessions": 0})


if __name__ == "__main__":
    unittest.main()
