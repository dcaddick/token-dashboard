"""Parse cumulative Codex token snapshots and maintain session totals."""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

from .db import connect

TOKEN_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)

UPSERT_SESSION = """
INSERT INTO provider_sessions (
  provider, session_id, path, mtime, bytes_read, day, input_tokens,
  output_tokens, cached_input_tokens, cache_create_tokens,
  reasoning_output_tokens, accuracy, updated_at
) VALUES (
  'codex', :session_id, :path, :mtime, :bytes_read, :day, :input_tokens,
  :output_tokens, :cached_input_tokens, 0,
  :reasoning_output_tokens, 'exact', :updated_at
)
ON CONFLICT(provider, session_id) DO UPDATE SET
  path=excluded.path,
  mtime=excluded.mtime,
  bytes_read=excluded.bytes_read,
  day=excluded.day,
  input_tokens=excluded.input_tokens,
  output_tokens=excluded.output_tokens,
  cached_input_tokens=excluded.cached_input_tokens,
  cache_create_tokens=excluded.cache_create_tokens,
  reasoning_output_tokens=excluded.reasoning_output_tokens,
  accuracy=excluded.accuracy,
  updated_at=excluded.updated_at
"""


def local_day(timestamp: str) -> str:
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return parsed.astimezone().date().isoformat()


def _token_count(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _usage_delta(current: dict, previous: dict) -> dict:
    return {
        key: max(_token_count(current.get(key)) - _token_count(previous.get(key)), 0)
        for key in TOKEN_KEYS
    }


def _parse_complete_lines(path: Path) -> tuple[Optional[dict], int]:
    session_id = None
    final = None
    active_model = "unknown-codex"
    previous = {}
    contributions = {}
    end_offset = 0

    with open(path, "rb") as source:
        while True:
            raw = source.readline()
            if not raw:
                break
            if not raw.endswith(b"\n"):
                break
            end_offset = source.tell()
            try:
                record = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(record, dict):
                continue

            payload = record.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            if record.get("type") == "session_meta":
                session_id = payload.get("id") or session_id
                continue
            if record.get("type") == "turn_context":
                active_model = str(payload.get("model") or "unknown-codex")
                continue
            if record.get("type") != "event_msg" or payload.get("type") != "token_count":
                continue

            info = payload.get("info") or {}
            usage = info.get("total_token_usage") if isinstance(info, dict) else None
            timestamp = record.get("timestamp")
            if not isinstance(usage, dict) or not timestamp:
                continue
            try:
                day = local_day(timestamp)
            except (TypeError, ValueError):
                continue
            current = {
                "timestamp": timestamp,
                "day": day,
                "input_tokens": _token_count(usage.get("input_tokens")),
                "cached_input_tokens": _token_count(usage.get("cached_input_tokens")),
                "output_tokens": _token_count(usage.get("output_tokens")),
                "reasoning_output_tokens": _token_count(
                    usage.get("reasoning_output_tokens")
                ),
            }
            delta = _usage_delta(current, previous)
            if any(delta.values()):
                key = (active_model, day)
                total = contributions.setdefault(key, {name: 0 for name in TOKEN_KEYS})
                for name in TOKEN_KEYS:
                    total[name] += delta[name]
            previous = current
            final = current

    if final is None:
        return None, end_offset
    final["session_id"] = str(session_id or path.stem)
    return {
        "session": final,
        "contributions": [
            {"model": model, "day": day, **usage}
            for (model, day), usage in sorted(contributions.items())
        ],
    }, end_offset


def parse_codex_session(path: Union[str, Path]) -> Optional[dict]:
    """Return the final complete cumulative token snapshot for one session."""
    parsed, _ = _parse_complete_lines(Path(path))
    return parsed["session"] if parsed else None


def parse_codex_model_usage(path: Union[str, Path]) -> list[dict]:
    """Return exact model/day deltas from cumulative Codex token snapshots."""
    parsed, _ = _parse_complete_lines(Path(path))
    return parsed["contributions"] if parsed else []


def _marker_path(path: Path) -> str:
    return f"codex:{path}"


def _delete_session(conn, session_id: str) -> None:
    conn.execute(
        "DELETE FROM provider_sessions WHERE provider='codex' AND session_id=?",
        (session_id,),
    )
    conn.execute("DELETE FROM codex_model_usage WHERE session_id=?", (session_id,))


def _replace_model_usage(conn, session_id: str, contributions: list[dict]) -> None:
    conn.execute("DELETE FROM codex_model_usage WHERE session_id=?", (session_id,))
    now = time.time()
    conn.executemany(
        """
        INSERT INTO codex_model_usage (
          session_id, model, day, input_tokens, output_tokens,
          cached_input_tokens, reasoning_output_tokens, accuracy, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'exact', ?)
        """,
        [
            (
                session_id,
                row["model"],
                row["day"],
                row["input_tokens"],
                row["output_tokens"],
                row["cached_input_tokens"],
                row["reasoning_output_tokens"],
                now,
            )
            for row in contributions
        ],
    )


def _is_under_root(path: str, root: Path) -> bool:
    try:
        return Path(path).resolve().is_relative_to(root)
    except (OSError, ValueError):
        return False


def scan_codex_dir(
    sessions_root: Union[str, Path], db_path: Union[str, Path]
) -> dict:
    """Upsert changed Codex sessions and return changed file/session counts."""
    root = Path(sessions_root).resolve()
    totals = {"files": 0, "sessions": 0}
    if not root.exists() or not root.is_dir():
        return totals

    with connect(db_path) as conn:
        paths = sorted(path.resolve() for path in root.rglob("*.jsonl"))
        seen_paths = {str(path) for path in paths}

        stale_sessions = [
            row
            for row in conn.execute(
                "SELECT session_id, path FROM provider_sessions WHERE provider='codex'"
            )
            if _is_under_root(row["path"], root) and row["path"] not in seen_paths
        ]
        for row in stale_sessions:
            _delete_session(conn, row["session_id"])

        for row in list(conn.execute("SELECT path FROM files WHERE path LIKE 'codex:%'")):
            source_path = row["path"][len("codex:"):]
            if _is_under_root(source_path, root) and source_path not in seen_paths:
                conn.execute("DELETE FROM files WHERE path=?", (row["path"],))

        # If a canonical duplicate disappeared, unchanged duplicate files need
        # one re-evaluation so the deterministic next path can be promoted.
        missing_model_usage = conn.execute(
            """
            SELECT 1
              FROM provider_sessions p
             WHERE p.provider='codex'
               AND NOT EXISTS (
                 SELECT 1 FROM codex_model_usage u
                  WHERE u.session_id=p.session_id
               )
             LIMIT 1
            """
        ).fetchone()
        force_rescan = bool(stale_sessions or missing_model_usage)
        accepted_session_ids = set()
        for path in paths:
            try:
                stat = path.stat()
            except OSError:
                continue
            marker = conn.execute(
                """
                SELECT mtime, bytes_read
                  FROM files
                 WHERE path=?
                """,
                (_marker_path(path),),
            ).fetchone()
            if (
                not force_rescan
                and marker
                and marker["mtime"] == stat.st_mtime
                and marker["bytes_read"] == stat.st_size
            ):
                continue

            totals["files"] += 1
            try:
                parsed, _ = _parse_complete_lines(path)
            except OSError:
                continue
            conn.execute(
                """
                INSERT OR REPLACE INTO files (path, mtime, bytes_read, scanned_at)
                VALUES (?, ?, ?, ?)
                """,
                (_marker_path(path), stat.st_mtime, stat.st_size, time.time()),
            )
            if parsed is None:
                stale = conn.execute(
                    "SELECT session_id FROM provider_sessions WHERE provider='codex' AND path=?",
                    (str(path),),
                ).fetchone()
                if stale:
                    _delete_session(conn, stale["session_id"])
                    force_rescan = True
                continue
            row = parsed["session"]

            # A file's metadata ID can change after replacement. Remove the
            # stale identity for this path before upserting the current one.
            stale = conn.execute(
                """
                SELECT session_id FROM provider_sessions
                 WHERE provider='codex' AND path=? AND session_id!=?
                """,
                (str(path), row["session_id"]),
            ).fetchone()
            if stale:
                _delete_session(conn, stale["session_id"])
                force_rescan = True
            canonical = conn.execute(
                """
                SELECT path FROM provider_sessions
                 WHERE provider='codex' AND session_id=?
                """,
                (row["session_id"],),
            ).fetchone()
            if canonical and canonical["path"] < str(path):
                continue
            conn.execute(
                UPSERT_SESSION,
                {
                    **row,
                    "path": str(path),
                    "mtime": stat.st_mtime,
                    # Codex sessions are reparsed as cumulative snapshots.
                    # Track the observed file size so an unchanged partial
                    # tail does not cause repeated scans; later growth still
                    # invalidates this marker and reparses the whole file.
                    "bytes_read": stat.st_size,
                    "updated_at": time.time(),
                },
            )
            _replace_model_usage(conn, row["session_id"], parsed["contributions"])
            accepted_session_ids.add(row["session_id"])
        totals["sessions"] = len(accepted_session_ids)
        conn.commit()
    return totals
