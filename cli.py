"""Token Dashboard CLI entrypoint."""
from __future__ import annotations

import argparse
import os
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

from token_dashboard.db import init_db, default_db_path, overview_totals
from token_dashboard.burn import rebuild_daily_usage
from token_dashboard.codex_scanner import scan_codex_dir
from token_dashboard.scanner import scan_dir
from token_dashboard.tips import all_tips


def _db_path(args) -> str:
    return args.db or os.environ.get("TOKEN_DASHBOARD_DB") or str(default_db_path())


def _projects(args) -> list[str]:
    if getattr(args, "projects_dir", None):
        roots = args.projects_dir if isinstance(args.projects_dir, (list, tuple)) else [args.projects_dir]
        return [r for r in roots if r]

    env = os.environ.get("TOKEN_DASHBOARD_PROJECTS_DIRS") or os.environ.get("CLAUDE_PROJECTS_DIR")
    if env:
        return [r for r in env.split(os.pathsep) if r]
    return [str(Path.home() / ".claude" / "projects")]


def _codex_sessions(args) -> str:
    return (
        getattr(args, "codex_sessions_dir", None)
        or os.environ.get("CODEX_SESSIONS_DIR")
        or str(Path.home() / ".codex" / "sessions")
    )


def _refresh_usage(args, db: str) -> dict:
    claude = scan_dir(_projects(args), db)
    codex = scan_codex_dir(_codex_sessions(args), db)
    burn = rebuild_daily_usage(db)
    return {"claude": claude, "codex": codex, "burn": burn}


def _today_range():
    now = datetime.now(timezone.utc)
    start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc).isoformat()
    end = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    return start, end


def cmd_scan(args):
    db = _db_path(args)
    init_db(db)
    n = _refresh_usage(args, db)
    claude = n["claude"]
    codex = n["codex"]
    print(
        "Token Dashboard: "
        f"Claude scanned {claude['files']} files, {claude['messages']} messages, "
        f"{claude['tools']} tool calls; "
        f"Codex scanned {codex['files']} files, {codex['sessions']} sessions"
    )


def cmd_today(args):
    db = _db_path(args)
    init_db(db)
    s, e = _today_range()
    t = overview_totals(db, since=s, until=e)
    print("Token Dashboard — today")
    print(f"  sessions: {t['sessions']}    turns: {t['turns']}")
    print(f"  input:    {t['input_tokens']:>12,}    output: {t['output_tokens']:>12,}")
    print(f"  cache rd: {t['cache_read_tokens']:>12,}    cache cr: {t['cache_create_5m_tokens']+t['cache_create_1h_tokens']:>12,}")


def cmd_stats(args):
    db = _db_path(args)
    init_db(db)
    t = overview_totals(db)
    print("Token Dashboard — all time")
    print(f"  sessions: {t['sessions']}    turns: {t['turns']}")
    print(f"  input:    {t['input_tokens']:>12,}    output: {t['output_tokens']:>12,}")


def cmd_tips(args):
    db = _db_path(args)
    init_db(db)
    tips = all_tips(db)
    if not tips:
        print("Token Dashboard: no suggestions")
        return
    for tip in tips:
        print(f"[{tip['category']}] {tip['title']}")
        print(f"  {tip['body']}\n")


def cmd_dashboard(args):
    db = _db_path(args)
    init_db(db)
    if not args.no_scan:
        _refresh_usage(args, db)
    from token_dashboard.server import run

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8080"))
    url = f"http://{host}:{port}/"
    if not args.no_open:
        webbrowser.open(url)
    print(f"Token Dashboard listening on {url}")
    run(host, port, db, _projects(args), _codex_sessions(args))


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", help="SQLite path (default ~/.claude/token-dashboard.db)")
    common.add_argument("--projects-dir", action="append", help="JSONL root; repeat to scan multiple roots (default ~/.claude/projects)")
    common.add_argument("--codex-sessions-dir", help="Codex JSONL root (default ~/.codex/sessions)")

    p = argparse.ArgumentParser(prog="token-dashboard", description="Local Claude Code usage dashboard", parents=[common])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan",  parents=[common]).set_defaults(func=cmd_scan)
    sub.add_parser("today", parents=[common]).set_defaults(func=cmd_today)
    sub.add_parser("stats", parents=[common]).set_defaults(func=cmd_stats)
    sub.add_parser("tips",  parents=[common]).set_defaults(func=cmd_tips)
    d = sub.add_parser("dashboard", parents=[common])
    d.add_argument("--no-scan", action="store_true")
    d.add_argument("--no-open", action="store_true")
    d.set_defaults(func=cmd_dashboard)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
