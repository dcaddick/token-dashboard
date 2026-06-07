import os
import sqlite3
import tempfile
import unittest
from token_dashboard.db import init_db, connect


class InitDbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.db")

    def test_init_creates_expected_tables(self):
        init_db(self.db_path)
        with sqlite3.connect(self.db_path) as c:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        expected = {
            "files", "messages", "tool_calls", "plan", "dismissed_tips",
            "provider_sessions", "daily_provider_usage",
        }
        self.assertTrue(expected.issubset(tables), f"Missing: {expected - tables}")

    def test_provider_usage_keys_are_unique(self):
        init_db(self.db_path)
        with sqlite3.connect(self.db_path) as c:
            c.execute("""
              INSERT INTO provider_sessions
                (provider, session_id, path, mtime, bytes_read, day, input_tokens,
                 output_tokens, cached_input_tokens, cache_create_tokens,
                 reasoning_output_tokens, accuracy, updated_at)
              VALUES ('codex','s','p',1,1,'2026-06-06',1,2,0,0,0,'exact',1)
            """)
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute("""
                  INSERT INTO provider_sessions
                    (provider, session_id, path, mtime, bytes_read, day, input_tokens,
                     output_tokens, cached_input_tokens, cache_create_tokens,
                     reasoning_output_tokens, accuracy, updated_at)
                  VALUES ('codex','s','p2',2,2,'2026-06-07',1,2,0,0,0,'exact',2)
                """)

    def test_init_is_idempotent(self):
        init_db(self.db_path)
        init_db(self.db_path)

    def test_connect_returns_row_factory(self):
        init_db(self.db_path)
        with connect(self.db_path) as c:
            r = c.execute("SELECT 1 AS one").fetchone()
        self.assertEqual(r["one"], 1)


if __name__ == "__main__":
    unittest.main()
