# Known Limitations

None of these are blockers — the dashboard still gives you useful information. They're the rough edges you'll notice if you look hard.

## Skills token counts are partial

The Skills route shows every skill Claude Code invoked, how many times, across how many sessions, and when. The **tokens-per-call** column is populated only for skills whose `SKILL.md` lives under `~/.claude/skills/`, `~/.claude/scheduled-tasks/`, or `~/.claude/plugins/`. Skills registered elsewhere (project-local `.claude/skills/`, or invocations that go through the `Task` tool with a skill-shaped `subagent_type`) show invocation counts but leave the token column blank.

It's still a useful view — you can see which skills dominate your session time — just don't expect a complete per-skill token cost. PRs to broaden the catalog scan welcome.

## Cost for Pro / Max / Max-20x users is shown as API-equivalent, not subscription value

The Settings route lets you select your pricing plan, but the Overview cost number is always the API-equivalent (what the same usage would have cost on pay-per-token rates). If you're on Pro you pay a flat $20/month regardless of how much of that API-equivalent number you rack up. We don't do "subscription ROI" math yet — Anthropic doesn't publish per-plan rate limits as public JSON, and faking it would be worse than not doing it.

## Cowork sessions are invisible

If you use Claude's Cowork mode (server-side sessions, not local `claude` CLI), those sessions don't write JSONL to `~/.claude/projects/` and the dashboard can't see them.

## Non-standard model names get tier-fallback pricing

If a transcript references a model ID not in `pricing.json` (e.g. a future snapshot that isn't in our table yet), cost is estimated from the tier substring (`opus` / `sonnet` / `haiku`) in the name. The UI marks these as `estimated: true`. If the model name contains none of those substrings, cost is reported as null.

## First scan can be slow

The first `python3 cli.py scan` on a heavy user's machine can read tens of MB across hundreds of JSONLs. Subsequent scans are incremental (mtime + byte-offset tracking in the `files` table), so they're fast.

## Token Burn normalization and day attribution

The Burn tab uses exact local counters for Claude Code and Codex, but their
cache fields have different meanings. **Workload** represents total model
processing; **Billable** is a provider-normalized token comparison that removes
Claude cache reads and Codex cached input. It is not a universal cost estimate.

Codex writes cumulative usage snapshots per session. The dashboard uses the
final complete snapshot and attributes the whole session to that event's local
calendar day. A session spanning midnight is not split across days.

For model lanes, Codex usage is attributed using the non-negative difference
between consecutive cumulative snapshots. Each delta is assigned to the active
model from the latest complete `turn_context` and to the token event's local
day. Snapshot resets are clamped to zero. When model context is missing, exact
usage remains visible under `Unknown Codex`; missing Claude model identities
appear under `Unknown Claude`.

ChatGPT ingestion is not implemented yet. The provider-neutral data model can
support it later, including estimated usage where exact local counters are
unavailable.

## Running two dashboards against the same DB

Both will fight over the SQLite file and you'll see inconsistent numbers and occasional `database is locked` errors. Only run one at a time. If you want to view the dashboard from a second device, use `HOST=0.0.0.0` on the one running machine and point the second device's browser at it.
