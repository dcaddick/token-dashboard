import gc
import http.server
import json
import os
import shutil
import socket
import tempfile
import threading
import unittest
import urllib.request
import warnings
from unittest import mock

from token_dashboard.db import connect, init_db
from token_dashboard import server
from token_dashboard.server import build_handler


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tmp = self.temp_dir.name
        self.db = os.path.join(self.tmp, "t.db")
        self.codex = os.path.join(self.tmp, "codex-sessions")
        os.makedirs(self.codex)
        shutil.copy(
            os.path.join(
                os.path.dirname(__file__), "fixtures", "codex_session.jsonl"
            ),
            os.path.join(self.codex, "session.jsonl"),
        )
        init_db(self.db)
        with connect(self.db) as c:
            c.execute("INSERT INTO messages (uuid, parent_uuid, session_id, project_slug, type, timestamp, model, input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens, prompt_text, prompt_chars) VALUES ('u',NULL,'s','p','user','2026-04-19T00:00:00Z',NULL,0,0,0,0,0,'hi',2)")
            c.execute("INSERT INTO messages (uuid, parent_uuid, session_id, project_slug, type, timestamp, model, input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens) VALUES ('a','u','s','p','assistant','2026-04-19T00:00:01Z','claude-haiku-4-5',1,1,0,0,0)")
            c.execute(
                """
                INSERT INTO daily_provider_usage (
                  provider, day, workload_tokens, billable_tokens, accuracy,
                  updated_at
                ) VALUES ('claude', '2026-04-19', 100, 20, 'exact', 1)
                """
            )
            c.commit()
        self.port = _free_port()
        H = build_handler(
            self.db,
            projects_dir="/nonexistent",
            codex_sessions_dir=self.codex,
        )
        self.httpd = http.server.HTTPServer(("127.0.0.1", self.port), H)
        self.server_thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True
        )
        self.server_thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.server_thread.join(timeout=2)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            gc.collect()
        self.temp_dir.cleanup()

    def _get(self, path):
        return urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}").read()

    def test_index_html(self):
        body = self._get("/")
        self.assertIn(b"Token Dashboard", body)

    def test_overview_json(self):
        body = json.loads(self._get("/api/overview"))
        self.assertIn("sessions", body)
        self.assertEqual(body["sessions"], 1)

    def test_prompts_json(self):
        body = json.loads(self._get("/api/prompts?limit=10"))
        self.assertIsInstance(body, list)

    def test_projects_json(self):
        body = json.loads(self._get("/api/projects"))
        self.assertIsInstance(body, list)
        self.assertEqual(body[0]["project_slug"], "p")

    def test_plan_json(self):
        body = json.loads(self._get("/api/plan"))
        self.assertIn("plan", body)
        self.assertIn("pricing", body)

    def test_burn_json(self):
        body = json.loads(self._get("/api/burn?metric=billable"))
        self.assertEqual(body["metric"], "billable")
        self.assertEqual(body["total"], 20)
        self.assertIn("providers", body)

    def test_burn_invalid_metric_falls_back(self):
        body = json.loads(self._get("/api/burn?metric=invalid"))
        self.assertEqual(body["metric"], "workload")
        self.assertEqual(body["total"], 100)

    def test_burn_passes_date_filters(self):
        body = json.loads(
            self._get("/api/burn?since=2026-04-20&until=2026-04-21")
        )
        self.assertEqual(body["total"], 0)

    def test_scan_refreshes_all_providers_and_burn_usage(self):
        body = json.loads(self._get("/api/scan"))

        self.assertEqual(body["codex"]["sessions"], 1)
        self.assertEqual(body["burn"], {"claude": 1, "codex": 1})
        self.assertTrue(body["usage_changed"])
        burn = json.loads(self._get("/api/burn"))
        self.assertEqual(
            {row["provider"] for row in burn["providers"]},
            {"claude", "codex"},
        )

    def test_meaningful_changes_ignore_scanned_files_without_usage_change(self):
        self.assertFalse(
            server._has_meaningful_changes(
                {
                    "claude": {"files": 1, "messages": 0, "tools": 0},
                    "codex": {"files": 1, "sessions": 0},
                    "burn": {"claude": 1, "codex": 0},
                    "usage_changed": False,
                }
            )
        )

    def test_meaningful_changes_include_burn_deletion_only_changes(self):
        self.assertTrue(
            server._has_meaningful_changes(
                {
                    "claude": {"files": 0, "messages": 0, "tools": 0},
                    "codex": {"files": 0, "sessions": 0},
                    "burn": {"claude": 1, "codex": 0},
                    "usage_changed": True,
                }
            )
        )

    def test_meaningful_changes_include_claude_message_only_additions(self):
        self.assertTrue(
            server._has_meaningful_changes(
                {
                    "claude": {"files": 1, "messages": 1, "tools": 0},
                    "codex": {"files": 0, "sessions": 0},
                    "burn": {"claude": 1, "codex": 0},
                    "usage_changed": False,
                }
            )
        )

    def test_refresh_detects_deleted_codex_usage(self):
        first = server._refresh_usage(self.db, "/nonexistent", self.codex)
        os.remove(os.path.join(self.codex, "session.jsonl"))
        second = server._refresh_usage(self.db, "/nonexistent", self.codex)

        self.assertTrue(first["usage_changed"])
        self.assertTrue(second["usage_changed"])
        self.assertEqual(second["codex"], {"files": 0, "sessions": 0})

    def test_background_scan_loop_emits_only_for_usage_changes(self):
        subscriber = server.EVENTS.subscribe()
        changed = {
            "claude": {"files": 0, "messages": 0, "tools": 0},
            "codex": {"files": 0, "sessions": 0},
            "burn": {"claude": 0, "codex": 0},
            "usage_changed": True,
        }
        with mock.patch.object(server, "_refresh_usage", return_value=changed), \
                mock.patch.object(server.time, "sleep", side_effect=StopIteration):
            with self.assertRaises(StopIteration):
                server._scan_loop(self.db, "/nonexistent", interval=0)

        event = subscriber.get_nowait()
        self.assertEqual(event["type"], "scan")
        self.assertEqual(event["n"], changed)

        unchanged = {**changed, "usage_changed": False}
        with mock.patch.object(server, "_refresh_usage", return_value=unchanged), \
                mock.patch.object(server.time, "sleep", side_effect=StopIteration):
            with self.assertRaises(StopIteration):
                server._scan_loop(self.db, "/nonexistent", interval=0)
        self.assertTrue(subscriber.empty())
        server.EVENTS.unsubscribe(subscriber)

    def test_broadcaster_delivers_to_every_subscriber(self):
        broadcaster = server.EventBroadcaster()
        first = broadcaster.subscribe()
        second = broadcaster.subscribe()
        event = {"type": "scan"}

        broadcaster.publish(event)

        self.assertEqual(first.get_nowait(), event)
        self.assertEqual(second.get_nowait(), event)
        broadcaster.unsubscribe(first)
        broadcaster.publish({"type": "later"})
        self.assertTrue(first.empty())
        self.assertEqual(second.get_nowait(), {"type": "later"})
        broadcaster.unsubscribe(second)

    def test_refresh_usage_serializes_concurrent_attempts(self):
        active = 0
        maximum_active = 0
        counter_lock = threading.Lock()
        start_gate = threading.Barrier(3)

        def scan(*_args):
            nonlocal active, maximum_active
            with counter_lock:
                active += 1
                maximum_active = max(maximum_active, active)
            threading.Event().wait(0.03)
            with counter_lock:
                active -= 1
            return {"files": 0, "messages": 0, "tools": 0}

        def refresh():
            start_gate.wait()
            server._refresh_usage(self.db, "/nonexistent", self.codex)

        with mock.patch.object(server, "scan_dir", side_effect=scan), \
                mock.patch.object(
                    server, "scan_codex_dir",
                    return_value={"files": 0, "sessions": 0},
                ), mock.patch.object(
                    server, "rebuild_daily_usage",
                    return_value={"claude": 0, "codex": 0},
                ):
            threads = [threading.Thread(target=refresh) for _ in range(2)]
            for thread in threads:
                thread.start()
            start_gate.wait()
            for thread in threads:
                thread.join(timeout=2)

        self.assertEqual(maximum_active, 1)
        self.assertTrue(all(not thread.is_alive() for thread in threads))

    def test_run_binds_server_before_starting_scan_thread(self):
        order = []
        fake_httpd = mock.Mock()
        fake_httpd.serve_forever.side_effect = lambda: order.append("serve")
        fake_thread = mock.Mock()
        fake_thread.start.side_effect = lambda: order.append("thread")

        with mock.patch.object(
            server.http.server,
            "ThreadingHTTPServer",
            side_effect=lambda *_args: order.append("bind") or fake_httpd,
        ), mock.patch.object(
            server.threading, "Thread", return_value=fake_thread
        ):
            server.run("127.0.0.1", 0, self.db, "/nonexistent", self.codex)

        self.assertEqual(order, ["bind", "thread", "serve"])

    def test_head_returns_200_not_501(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/", method="HEAD")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read(), b"")

    def test_head_api_endpoint(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/overview", method="HEAD")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read(), b"")


if __name__ == "__main__":
    unittest.main()
