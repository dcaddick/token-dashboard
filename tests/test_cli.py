import gc
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import warnings
from argparse import Namespace
from unittest import mock

import cli
from token_dashboard.db import connect

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tmp = self.temp_dir.name
        self.proj = os.path.join(self.tmp, "projects")
        os.makedirs(os.path.join(self.proj, "demo"))
        with open(os.path.join(self.proj, "demo", "s.jsonl"), "w", encoding="utf-8") as f:
            f.write('{"type":"user","uuid":"u1","sessionId":"s1","timestamp":"2026-04-19T00:00:00Z","isSidechain":false,"message":{"role":"user","content":"hi"}}\n')
            f.write('{"type":"assistant","uuid":"a1","parentUuid":"u1","sessionId":"s1","timestamp":"2026-04-19T00:00:01Z","isSidechain":false,"message":{"model":"claude-haiku-4-5","usage":{"input_tokens":1,"output_tokens":1}}}\n')
        self.db = os.path.join(self.tmp, "t.db")

    def tearDown(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            gc.collect()
        self.temp_dir.cleanup()

    def _run(self, *args, env=None):
        env = {
            **os.environ,
            "TOKEN_DASHBOARD_DB": self.db,
            "CODEX_SESSIONS_DIR": os.path.join(self.tmp, "missing-codex"),
            **(env or {}),
        }
        return subprocess.run(
            [sys.executable, "cli.py", *args],
            cwd=ROOT, env=env, capture_output=True, text=True,
        )

    def test_scan_then_today(self):
        r1 = self._run("scan", "--projects-dir", self.proj)
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertIn("scanned", r1.stdout)
        r2 = self._run("today")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertIn("Token Dashboard", r2.stdout)

    def test_stats(self):
        self._run("scan", "--projects-dir", self.proj)
        r = self._run("stats")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("all time", r.stdout)

    def test_tips_runs_without_data(self):
        r = self._run("tips")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no suggestions", r.stdout)

    def test_scan_refreshes_codex_and_burn_usage(self):
        codex = os.path.join(self.tmp, "codex-sessions")
        os.makedirs(codex)
        shutil.copy(
            os.path.join(ROOT, "tests", "fixtures", "codex_session.jsonl"),
            os.path.join(codex, "session.jsonl"),
        )

        r = self._run(
            "scan",
            "--projects-dir",
            self.proj,
            "--codex-sessions-dir",
            codex,
        )

        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Claude scanned", r.stdout)
        self.assertIn("Codex scanned", r.stdout)
        with connect(self.db) as c:
            providers = {
                row[0] for row in c.execute(
                    "SELECT provider FROM daily_provider_usage"
                )
            }
        self.assertEqual(providers, {"claude", "codex"})

    def test_scan_uses_codex_sessions_environment_default(self):
        codex = os.path.join(self.tmp, "codex-env")
        os.makedirs(codex)
        shutil.copy(
            os.path.join(ROOT, "tests", "fixtures", "codex_session.jsonl"),
            os.path.join(codex, "session.jsonl"),
        )

        r = self._run(
            "scan",
            "--projects-dir",
            self.proj,
            env={"CODEX_SESSIONS_DIR": codex},
        )

        self.assertEqual(r.returncode, 0, r.stderr)
        with connect(self.db) as c:
            count = c.execute(
                "SELECT COUNT(*) FROM provider_sessions WHERE provider='codex'"
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_scan_accepts_repeated_projects_dir_roots(self):
        second = os.path.join(self.tmp, "second-projects")
        os.makedirs(os.path.join(second, "other"))
        with open(
            os.path.join(second, "other", "s2.jsonl"), "w", encoding="utf-8"
        ) as f:
            f.write(
                '{"type":"assistant","uuid":"a2","sessionId":"s2",'
                '"timestamp":"2026-04-20T00:00:01Z","isSidechain":false,'
                '"message":{"model":"claude-haiku-4-5",'
                '"usage":{"input_tokens":2,"output_tokens":3}}}\n'
            )

        r = self._run(
            "scan",
            "--projects-dir",
            self.proj,
            "--projects-dir",
            second,
        )

        self.assertEqual(r.returncode, 0, r.stderr)
        with connect(self.db) as c:
            projects = {
                row[0] for row in c.execute(
                    "SELECT DISTINCT project_slug FROM messages"
                )
            }
        self.assertEqual(projects, {"demo", "other"})

    def test_dashboard_initial_refreshes_before_starting_server(self):
        args = Namespace(
            db=self.db,
            projects_dir=[self.proj],
            codex_sessions_dir=os.path.join(self.tmp, "codex"),
            no_scan=False,
            no_open=True,
        )
        with mock.patch.dict(
            os.environ, {"HOST": "127.0.0.1", "PORT": "8080"}
        ), mock.patch.object(cli, "_refresh_usage") as refresh, mock.patch(
            "token_dashboard.server.run"
        ) as run:
            cli.cmd_dashboard(args)

        refresh.assert_called_once_with(args, self.db)
        run.assert_called_once_with(
            "127.0.0.1",
            8080,
            self.db,
            [self.proj],
            args.codex_sessions_dir,
        )


if __name__ == "__main__":
    unittest.main()
