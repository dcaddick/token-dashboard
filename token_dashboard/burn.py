"""Provider-neutral daily token usage aggregation and Burn summaries."""
from __future__ import annotations

import time
from collections import defaultdict
from datetime import date, datetime, timedelta

from token_dashboard.db import connect


def local_day(timestamp: str) -> str:
    """Convert an ISO timestamp to the machine's local calendar day."""
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return parsed.astimezone().date().isoformat()


def _tokens(row: dict, key: str) -> int:
    return int(row.get(key) or 0)


def normalize_claude(row: dict) -> dict:
    """Apply workload and billable-equivalent rules to one Claude usage row."""
    inp = _tokens(row, "input_tokens")
    out = _tokens(row, "output_tokens")
    cached = _tokens(row, "cache_read_tokens")
    cache_create = _tokens(row, "cache_create_tokens")
    return {
        **row,
        "input_tokens": inp,
        "output_tokens": out,
        "cached_input_tokens": cached,
        "cache_create_tokens": cache_create,
        "reasoning_output_tokens": 0,
        "workload_tokens": inp + out + cached + cache_create,
        "billable_tokens": inp + out + cache_create,
        "accuracy": "exact",
    }


def normalize_codex(row: dict) -> dict:
    """Apply workload and billable-equivalent rules to one Codex usage row."""
    inp = _tokens(row, "input_tokens")
    cached = _tokens(row, "cached_input_tokens")
    out = _tokens(row, "output_tokens")
    reasoning = _tokens(row, "reasoning_output_tokens")
    return {
        **row,
        "input_tokens": inp,
        "output_tokens": out,
        "cached_input_tokens": cached,
        "cache_create_tokens": 0,
        "reasoning_output_tokens": reasoning,
        "workload_tokens": inp + out + reasoning,
        "billable_tokens": max(inp - cached, 0) + out + reasoning,
        "accuracy": "exact",
    }


def _add_usage(total: dict, row: dict, keys: tuple[str, ...]) -> None:
    for key in keys:
        total[key] += _tokens(row, key)


def rebuild_daily_usage(db_path) -> dict:
    """Replace Claude and Codex daily aggregates from current source rows."""
    claude_by_day = defaultdict(
        lambda: {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_create_tokens": 0,
        }
    )

    with connect(db_path) as conn:
        claude_rows = conn.execute(
            """
            SELECT timestamp, input_tokens, output_tokens, cache_read_tokens,
                   cache_create_5m_tokens + cache_create_1h_tokens
                     AS cache_create_tokens
              FROM messages
             WHERE type='assistant' AND timestamp IS NOT NULL
            """
        )
        for source in claude_rows:
            try:
                day = local_day(source["timestamp"])
            except (TypeError, ValueError):
                continue
            _add_usage(
                claude_by_day[day],
                dict(source),
                (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                    "cache_create_tokens",
                ),
            )

        codex_rows = conn.execute(
            """
            SELECT day,
                   SUM(input_tokens) AS input_tokens,
                   SUM(output_tokens) AS output_tokens,
                   SUM(cached_input_tokens) AS cached_input_tokens,
                   SUM(reasoning_output_tokens) AS reasoning_output_tokens
              FROM provider_sessions
             WHERE provider='codex'
             GROUP BY day
             ORDER BY day
            """
        ).fetchall()

        normalized = []
        for day, usage in sorted(claude_by_day.items()):
            normalized.append(("claude", day, normalize_claude(usage)))
        for source in codex_rows:
            row = dict(source)
            normalized.append(("codex", row["day"], normalize_codex(row)))

        updated_at = time.time()
        conn.execute(
            "DELETE FROM daily_provider_usage WHERE provider IN ('claude', 'codex')"
        )
        conn.executemany(
            """
            INSERT INTO daily_provider_usage (
              provider, day, input_tokens, output_tokens, cached_input_tokens,
              cache_create_tokens, reasoning_output_tokens, workload_tokens,
              billable_tokens, accuracy, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    provider,
                    day,
                    row["input_tokens"],
                    row["output_tokens"],
                    row["cached_input_tokens"],
                    row["cache_create_tokens"],
                    row["reasoning_output_tokens"],
                    row["workload_tokens"],
                    row["billable_tokens"],
                    row["accuracy"],
                    updated_at,
                )
                for provider, day, row in normalized
            ],
        )
        conn.commit()

    return {
        "claude": len(claude_by_day),
        "codex": len(codex_rows),
    }


def burn_summary(db_path, since=None, until=None, metric="workload") -> dict:
    """Build the stable multi-provider Burn response for a date range."""
    metric = metric if metric in {"workload", "billable"} else "workload"
    value_col = "workload_tokens" if metric == "workload" else "billable_tokens"

    where = []
    args = []
    if since:
        where.append("day >= ?")
        args.append(since)
    if until:
        where.append("day < ?")
        args.append(until)
    clause = f"WHERE {' AND '.join(where)}" if where else ""

    with connect(db_path) as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT provider, day, {value_col} AS tokens, accuracy
                  FROM daily_provider_usage
                  {clause}
                 ORDER BY day, provider
                """,
                args,
            )
        ]

    provider_totals = defaultdict(int)
    provider_accuracy = defaultdict(set)
    day_totals = defaultdict(int)
    day_providers = defaultdict(dict)
    weekly_totals = defaultdict(int)
    daily = []

    for row in rows:
        provider = row["provider"]
        day = row["day"]
        try:
            parsed_day = date.fromisoformat(day)
        except (TypeError, ValueError):
            continue
        tokens = int(row["tokens"] or 0)
        provider_totals[provider] += tokens
        provider_accuracy[provider].add(row["accuracy"])
        day_totals[day] += tokens
        day_providers[day][provider] = tokens
        daily.append({"day": day, "provider": provider, "tokens": tokens})

        week = parsed_day - timedelta(days=parsed_day.weekday())
        weekly_totals[week.isoformat()] += tokens

    providers = [
        {
            "provider": provider,
            "tokens": tokens,
            "accuracy": (
                "exact"
                if provider_accuracy[provider] == {"exact"}
                else "estimated"
            ),
        }
        for provider, tokens in provider_totals.items()
    ]
    providers.sort(key=lambda row: (-row["tokens"], row["provider"]))

    ranked_days = sorted(day_totals.items(), key=lambda item: (-item[1], item[0]))
    peak_days = [
        {
            "day": day,
            "tokens": tokens,
            "providers": dict(sorted(day_providers[day].items())),
        }
        for day, tokens in ranked_days[:10]
    ]
    peak_day = (
        {"day": ranked_days[0][0], "tokens": ranked_days[0][1]}
        if ranked_days
        else {"day": None, "tokens": 0}
    )
    today = datetime.now().astimezone().date().isoformat()

    return {
        "metric": metric,
        "total": sum(day_totals.values()),
        "day_to_date": day_totals.get(today, 0),
        "peak_day": peak_day,
        "providers": providers,
        "daily": daily,
        "weekly": [
            {"week": week, "tokens": tokens}
            for week, tokens in sorted(weekly_totals.items())
        ],
        "peak_days": peak_days,
    }
