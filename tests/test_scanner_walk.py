import os
import shutil
import sqlite3
import tempfile
import unittest

from token_dashboard.db import init_db
from token_dashboard.scanner import scan_dir

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


class WalkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        self.proj_root = os.path.join(self.tmp, "projects")
        proj_dir = os.path.join(self.proj_root, "C--work-sample")
        os.makedirs(proj_dir)
        shutil.copy(
            os.path.join(FIXTURE_DIR, "sample_session.jsonl"),
            os.path.join(proj_dir, "s1.jsonl"),
        )
        init_db(self.db)

    def test_scan_writes_messages_and_tools(self):
        n = scan_dir(self.proj_root, self.db)
        self.assertEqual(n["messages"], 3)
        self.assertEqual(n["tools"], 2)  # 1 tool_use + 1 tool_result
        with sqlite3.connect(self.db) as c:
            row = c.execute("SELECT project_slug FROM messages WHERE uuid='u1'").fetchone()
        self.assertEqual(row[0], "C--work-sample")

    def test_rescan_skips_unchanged_files(self):
        n1 = scan_dir(self.proj_root, self.db)
        n2 = scan_dir(self.proj_root, self.db)
        self.assertEqual(n1["messages"], 3)
        self.assertEqual(n2["messages"], 0)

    def test_rescan_picks_up_appended_lines(self):
        scan_dir(self.proj_root, self.db)
        path = os.path.join(self.proj_root, "C--work-sample", "s1.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write('{"type":"assistant","uuid":"a2","sessionId":"s1","timestamp":"2026-04-10T00:00:03Z","isSidechain":false,"message":{"model":"claude-haiku-4-5","usage":{"input_tokens":1,"output_tokens":1}}}\n')
        n2 = scan_dir(self.proj_root, self.db)
        self.assertEqual(n2["messages"], 1)

    def test_scan_accepts_multiple_roots(self):
        other_root = os.path.join(self.tmp, "extra-projects")
        other_dir = os.path.join(other_root, "other-model")
        os.makedirs(other_dir)
        src = os.path.join(FIXTURE_DIR, "sample_session.jsonl")
        dst = os.path.join(other_dir, "s2.jsonl")
        shutil.copy(src, dst)
        with open(dst, "r", encoding="utf-8") as f:
            text = f.read()
        text = text.replace('"uuid":"u1"', '"uuid":"u1-extra"')
        text = text.replace('"uuid":"a1"', '"uuid":"a1-extra"')
        text = text.replace('"uuid":"u2"', '"uuid":"u2-extra"')
        with open(dst, "w", encoding="utf-8") as f:
            f.write(text)

        n = scan_dir([self.proj_root, other_root], self.db)

        self.assertEqual(n["messages"], 6)
        self.assertEqual(n["tools"], 4)
        with sqlite3.connect(self.db) as c:
            count = c.execute("SELECT COUNT(*) FROM messages WHERE project_slug IN ('C--work-sample', 'other-model')").fetchone()[0]
        self.assertEqual(count, 6)


if __name__ == "__main__":
    unittest.main()
