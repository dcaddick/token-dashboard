import os
import tempfile
import unittest
from datetime import datetime

from token_dashboard.burn import (
    burn_summary,
    local_day,
    normalize_claude,
    normalize_codex,
    rebuild_daily_usage,
)
from token_dashboard.db import connect, init_db


class BurnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "burn.db")
        init_db(self.db)

    def _insert_message(
        self,
        uuid,
        timestamp,
        *,
        type="assistant",
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_create_5m_tokens=0,
        cache_create_1h_tokens=0,
        model=None,
    ):
        with connect(self.db) as c:
            c.execute(
                """
                INSERT INTO messages (
                  uuid, session_id, project_slug, type, timestamp,
                  input_tokens, output_tokens, cache_read_tokens,
                  cache_create_5m_tokens, cache_create_1h_tokens, model
                ) VALUES (?, 'session', 'project', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid,
                    type,
                    timestamp,
                    input_tokens,
                    output_tokens,
                    cache_read_tokens,
                    cache_create_5m_tokens,
                    cache_create_1h_tokens,
                    model,
                ),
            )
            c.commit()

    def _insert_codex(
        self,
        session_id,
        day,
        *,
        input_tokens=0,
        output_tokens=0,
        cached_input_tokens=0,
        reasoning_output_tokens=0,
    ):
        with connect(self.db) as c:
            c.execute(
                """
                INSERT INTO provider_sessions (
                  provider, session_id, path, mtime, bytes_read, day,
                  input_tokens, output_tokens, cached_input_tokens,
                  reasoning_output_tokens, accuracy, updated_at
                ) VALUES ('codex', ?, ?, 1, 1, ?, ?, ?, ?, ?, 'exact', 1)
                """,
                (
                    session_id,
                    f"{session_id}.jsonl",
                    day,
                    input_tokens,
                    output_tokens,
                    cached_input_tokens,
                    reasoning_output_tokens,
                ),
            )
            c.execute(
                """
                INSERT INTO codex_model_usage (
                  session_id, model, day, input_tokens, output_tokens,
                  cached_input_tokens, reasoning_output_tokens, accuracy, updated_at
                ) VALUES (?, 'unknown-codex', ?, ?, ?, ?, ?, 'exact', 1)
                """,
                (
                    session_id,
                    day,
                    input_tokens,
                    output_tokens,
                    cached_input_tokens,
                    reasoning_output_tokens,
                ),
            )
            c.commit()

    def _insert_daily(
        self, provider, day, workload, billable, accuracy="exact", model=None
    ):
        model = model or f"unknown-{provider}"
        with connect(self.db) as c:
            c.execute(
                """
                INSERT INTO daily_provider_usage (
                  provider, model, day, workload_tokens, billable_tokens, accuracy,
                  updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1)
                """,
                (provider, model, day, workload, billable, accuracy),
            )
            c.commit()

    def test_claude_metric_rules(self):
        row = normalize_claude(
            {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_tokens": 100,
                "cache_create_tokens": 20,
            }
        )
        self.assertEqual(row["cached_input_tokens"], 100)
        self.assertEqual(row["workload_tokens"], 135)
        self.assertEqual(row["billable_tokens"], 35)

    def test_codex_metric_rules_do_not_double_count_cached_input(self):
        row = normalize_codex(
            {
                "input_tokens": 250,
                "cached_input_tokens": 100,
                "output_tokens": 25,
                "reasoning_output_tokens": 5,
            }
        )
        self.assertEqual(row["workload_tokens"], 280)
        self.assertEqual(row["billable_tokens"], 180)

    def test_codex_billable_input_cannot_be_negative(self):
        row = normalize_codex(
            {
                "input_tokens": 10,
                "cached_input_tokens": 20,
                "output_tokens": 2,
                "reasoning_output_tokens": 1,
            }
        )
        self.assertEqual(row["billable_tokens"], 3)

    def test_local_day_uses_machine_local_calendar_date(self):
        timestamp = "2026-06-06T20:30:00Z"
        expected = datetime.fromisoformat(
            timestamp.replace("Z", "+00:00")
        ).astimezone().date().isoformat()
        self.assertEqual(local_day(timestamp), expected)

    def test_rebuild_combines_providers_and_uses_only_assistant_messages(self):
        timestamp = "2026-06-06T07:00:00Z"
        day = local_day(timestamp)
        self._insert_message(
            "assistant",
            timestamp,
            input_tokens=10,
            output_tokens=5,
            cache_read_tokens=100,
            cache_create_5m_tokens=12,
            cache_create_1h_tokens=8,
        )
        self._insert_message(
            "user",
            timestamp,
            type="user",
            input_tokens=999,
            output_tokens=999,
            cache_read_tokens=999,
            cache_create_5m_tokens=999,
        )
        self._insert_codex(
            "codex-1",
            day,
            input_tokens=250,
            cached_input_tokens=100,
            output_tokens=25,
            reasoning_output_tokens=5,
        )

        result = rebuild_daily_usage(self.db)

        self.assertEqual(result, {"claude": 1, "codex": 1})
        with connect(self.db) as c:
            rows = {
                row["provider"]: dict(row)
                for row in c.execute(
                    "SELECT * FROM daily_provider_usage ORDER BY provider"
                )
            }
        self.assertEqual(rows["claude"]["workload_tokens"], 135)
        self.assertEqual(rows["claude"]["billable_tokens"], 35)
        self.assertEqual(rows["codex"]["workload_tokens"], 280)
        self.assertEqual(rows["codex"]["billable_tokens"], 180)

    def test_rebuild_splits_claude_usage_by_model_and_preserves_mai(self):
        timestamp = "2026-06-06T07:00:00Z"
        self._insert_message("a", timestamp, input_tokens=10, model="claude-opus-4-7")
        self._insert_message("b", timestamp, input_tokens=20, model="mai-1")
        self._insert_message("c", timestamp, input_tokens=30, model=None)

        rebuild_daily_usage(self.db)

        with connect(self.db) as c:
            rows = c.execute("""
              SELECT model, workload_tokens FROM daily_provider_usage
               WHERE provider='claude' ORDER BY model
            """).fetchall()
        self.assertEqual(
            [(r["model"], r["workload_tokens"]) for r in rows],
            [("claude-opus-4-7", 10), ("mai-1", 20), ("unknown-claude", 30)],
        )

    def test_rebuild_rolls_up_codex_model_contributions(self):
        with connect(self.db) as c:
            c.execute("""
              INSERT INTO codex_model_usage (
                session_id, model, day, input_tokens, output_tokens,
                cached_input_tokens, reasoning_output_tokens, updated_at
              ) VALUES ('s','gpt-5','2026-06-06',100,10,40,2,1)
            """)
            c.commit()
        rebuild_daily_usage(self.db)
        with connect(self.db) as c:
            row = c.execute("""
              SELECT model, workload_tokens, billable_tokens
                FROM daily_provider_usage WHERE provider='codex'
            """).fetchone()
        self.assertEqual(tuple(row), ("gpt-5", 112, 72))

    def test_rebuild_groups_claude_by_local_day_not_utc_substring(self):
        timestamp = "2026-06-06T20:30:00Z"
        expected_day = local_day(timestamp)
        self._insert_message("assistant", timestamp, input_tokens=10)

        rebuild_daily_usage(self.db)

        with connect(self.db) as c:
            row = c.execute(
                "SELECT day FROM daily_provider_usage WHERE provider='claude'"
            ).fetchone()
        self.assertEqual(row["day"], expected_day)

    def test_rebuild_preserves_future_providers(self):
        self._insert_daily("chatgpt", "2026-06-06", 50, 40, "estimated")
        self._insert_message(
            "assistant", "2026-06-06T07:00:00Z", input_tokens=10
        )

        rebuild_daily_usage(self.db)

        with connect(self.db) as c:
            future = dict(
                c.execute(
                    "SELECT * FROM daily_provider_usage WHERE provider='chatgpt'"
                ).fetchone()
            )
        self.assertEqual(future["workload_tokens"], 50)
        self.assertEqual(future["accuracy"], "estimated")

    def test_rebuild_replaces_stale_claude_and_codex_rows(self):
        self._insert_daily("claude", "2020-01-01", 99, 99)
        self._insert_daily("codex", "2020-01-01", 99, 99)

        result = rebuild_daily_usage(self.db)

        self.assertEqual(result, {"claude": 0, "codex": 0})
        with connect(self.db) as c:
            count = c.execute(
                """
                SELECT COUNT(*) FROM daily_provider_usage
                WHERE provider IN ('claude', 'codex')
                """
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_burn_summary_combines_providers_and_builds_stable_shape(self):
        self._insert_daily("claude", "2026-06-06", 135, 35)
        self._insert_daily("codex", "2026-06-06", 280, 180)

        result = burn_summary(self.db, metric="workload")

        self.assertEqual(result["metric"], "workload")
        self.assertEqual(result["total"], 415)
        self.assertEqual(result["peak_day"], {"day": "2026-06-06", "tokens": 415})
        self.assertEqual(
            result["lanes"],
            [
                {"key": "provider:codex", "label": "OpenAI", "provider": "codex", "model": None, "tokens": 280, "accuracy": "exact"},
                {"key": "provider:claude", "label": "Anthropic", "provider": "claude", "model": None, "tokens": 135, "accuracy": "exact"},
            ],
        )
        self.assertEqual(
            result["daily"],
            [
                {"day": "2026-06-06", "lane": "provider:claude", "tokens": 135},
                {"day": "2026-06-06", "lane": "provider:codex", "tokens": 280},
            ],
        )
        self.assertEqual(result["weekly"], [{"week": "2026-06-01", "tokens": 415}])
        self.assertEqual(
            result["peak_days"],
            [
                {
                    "day": "2026-06-06",
                    "tokens": 415,
                    "lanes": {"provider:claude": 135, "provider:codex": 280},
                }
            ],
        )

    def test_burn_summary_uses_billable_metric(self):
        self._insert_daily("claude", "2026-06-06", 135, 35)
        self._insert_daily("codex", "2026-06-06", 280, 180)
        result = burn_summary(self.db, metric="billable")
        self.assertEqual(result["metric"], "billable")
        self.assertEqual(result["total"], 215)

    def test_burn_summary_filters_since_inclusive_and_until_exclusive(self):
        self._insert_daily("claude", "2026-06-05", 10, 10)
        self._insert_daily("claude", "2026-06-06", 20, 20)
        self._insert_daily("claude", "2026-06-07", 30, 30)

        result = burn_summary(
            self.db, since="2026-06-06", until="2026-06-07", metric="workload"
        )

        self.assertEqual(result["total"], 20)
        self.assertTrue(all(d["day"] == "2026-06-06" for d in result["daily"]))

    def test_invalid_metric_falls_back_to_workload(self):
        self._insert_daily("claude", "2026-06-06", 135, 35)
        result = burn_summary(self.db, metric="money")
        self.assertEqual(result["metric"], "workload")
        self.assertEqual(result["total"], 135)

    def test_model_group_returns_model_lanes_without_changing_totals(self):
        self._insert_daily("claude", "2026-06-06", 100, 80, model="claude-opus")
        self._insert_daily("codex", "2026-06-06", 200, 120, model="gpt-5")
        provider = burn_summary(self.db, group="provider")
        model = burn_summary(self.db, group="model")
        self.assertEqual(model["group"], "model")
        self.assertEqual(provider["total"], model["total"])
        self.assertEqual([lane["key"] for lane in model["lanes"]], [
            "model:codex:gpt-5", "model:claude:claude-opus",
        ])

    def test_invalid_group_falls_back_to_provider(self):
        self.assertEqual(burn_summary(self.db, group="invalid")["group"], "provider")

    def test_burn_summary_skips_malformed_stored_days(self):
        self._insert_daily("claude", "not-a-day", 999, 999)
        self._insert_daily("codex", "2026-06-06", 20, 10)

        result = burn_summary(self.db)

        self.assertEqual(result["total"], 20)
        self.assertEqual(
            result["daily"],
            [{"day": "2026-06-06", "lane": "provider:codex", "tokens": 20}],
        )
        self.assertEqual(result["weekly"], [{"week": "2026-06-01", "tokens": 20}])

    def test_weekly_totals_start_on_monday(self):
        self._insert_daily("claude", "2026-06-07", 10, 10)
        self._insert_daily("codex", "2026-06-08", 20, 20)
        result = burn_summary(self.db)
        self.assertEqual(
            result["weekly"],
            [
                {"week": "2026-06-01", "tokens": 10},
                {"week": "2026-06-08", "tokens": 20},
            ],
        )

    def test_peak_days_are_limited_to_ten(self):
        for index in range(12):
            self._insert_daily(
                "claude", f"2026-06-{index + 1:02d}", index + 1, index + 1
            )
        result = burn_summary(self.db)
        self.assertEqual(len(result["peak_days"]), 10)
        self.assertEqual(result["peak_days"][0]["tokens"], 12)
        self.assertEqual(result["peak_days"][-1]["tokens"], 3)

    def test_day_to_date_uses_local_day(self):
        today = datetime.now().astimezone().date().isoformat()
        self._insert_daily("claude", today, 42, 24)
        result = burn_summary(self.db, metric="workload")
        self.assertEqual(result["day_to_date"], 42)

    def test_empty_summary_is_safe(self):
        self.assertEqual(
            burn_summary(self.db),
            {
                "metric": "workload",
                "group": "provider",
                "total": 0,
                "day_to_date": 0,
                "peak_day": {"day": None, "tokens": 0},
                "lanes": [],
                "daily": [],
                "weekly": [],
                "peak_days": [],
            },
        )


if __name__ == "__main__":
    unittest.main()
