# Multi-Provider Token Burn Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a new Burn tab that compares exact local Claude Code and Codex daily token usage with workload and billable-equivalent views.

**Architecture:** Keep the current Claude message scanner and analytics intact. Add a focused Codex session scanner that stores one replaceable cumulative usage row per session, then rebuild a provider-neutral daily aggregate table from Claude messages and Codex session totals. Expose one `/api/burn` endpoint and render the new calendar-style page in a dedicated frontend route.

**Tech Stack:** Python 3 stdlib, SQLite, `unittest`, vanilla JavaScript, CSS Grid, ECharts.

---

## File Structure

- Create `token_dashboard/codex_scanner.py`: parse Codex cumulative token events and incrementally maintain session totals.
- Create `token_dashboard/burn.py`: normalize Claude and Codex usage, rebuild daily aggregates, and build Burn API responses.
- Create `tests/fixtures/codex_session.jsonl`: representative Codex cumulative-token fixture.
- Create `tests/test_codex_scanner.py`: parser, malformed-line, partial-line, and rescan tests.
- Create `tests/test_burn.py`: normalization, aggregation, range, metric, and combined-provider tests.
- Create `web/routes/burn.js`: Burn route, controls, weekly chart, heatmap lanes, and peak-day table.
- Modify `token_dashboard/db.py`: add provider session and daily usage tables.
- Modify `token_dashboard/server.py`: refresh provider aggregates and expose `/api/burn`.
- Modify `cli.py`: resolve the Codex sessions root and refresh Burn data during scan/dashboard startup.
- Modify `web/app.js`: register the Burn route.
- Modify `web/charts.js`: add a compact weekly-total line-chart helper if the existing helper cannot express the desired axis formatting.
- Modify `web/style.css`: add responsive Burn-page and heatmap styles.
- Modify `tests/test_db.py`, `tests/test_server.py`, and `tests/test_cli.py`: integration coverage.
- Modify `README.md` and `docs/KNOWN_LIMITATIONS.md`: document the Burn tab, source paths, metrics, and session-day attribution limitation.

Implementation must preserve the user's existing uncommitted changes in `README.md`, `cli.py`, `token_dashboard/scanner.py`, `token_dashboard/server.py`, `web/app.js`, `web/routes/overview.js`, `web/routes/skills.js`, `web/style.css`, and related tests. Read current file contents before each edit and stage only task-specific paths at each commit.

### Metric Rules

Use these exact rules throughout implementation:

```python
# Claude usage fields are distinct buckets.
claude_workload = input_tokens + output_tokens + cache_read_tokens + cache_create_tokens
claude_billable = input_tokens + output_tokens + cache_create_tokens

# Codex cached_input_tokens is a subset of input_tokens.
codex_workload = input_tokens + output_tokens + reasoning_output_tokens
codex_billable = max(input_tokens - cached_input_tokens, 0) + output_tokens + reasoning_output_tokens
```

Do not add Codex `cached_input_tokens` to workload a second time.

Use the machine's local calendar date for every daily aggregate. Convert ISO
timestamps with:

```python
def local_day(timestamp: str) -> str:
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return parsed.astimezone().date().isoformat()
```

Do not group by `substr(timestamp, 1, 10)`, because that groups by UTC day.

### Task 1: Add Provider Usage Schema

**Files:**
- Modify: `token_dashboard/db.py`
- Modify: `tests/test_db.py`

- [ ] **Step 1: Write failing schema tests**

Extend `InitDbTests.test_init_creates_expected_tables`:

```python
expected = {
    "files", "messages", "tool_calls", "plan", "dismissed_tips",
    "provider_sessions", "daily_provider_usage",
}
```

Add a uniqueness test:

```python
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
```

- [ ] **Step 2: Run schema tests to verify failure**

Run:

```powershell
python -m unittest tests.test_db -v
```

Expected: failure because `provider_sessions` and `daily_provider_usage` do not exist.

- [ ] **Step 3: Add the schema**

Append to `SCHEMA` in `token_dashboard/db.py`:

```sql
CREATE TABLE IF NOT EXISTS provider_sessions (
  provider                TEXT NOT NULL,
  session_id              TEXT NOT NULL,
  path                    TEXT NOT NULL,
  mtime                   REAL NOT NULL,
  bytes_read              INTEGER NOT NULL,
  day                     TEXT NOT NULL,
  input_tokens            INTEGER NOT NULL DEFAULT 0,
  output_tokens           INTEGER NOT NULL DEFAULT 0,
  cached_input_tokens     INTEGER NOT NULL DEFAULT 0,
  cache_create_tokens     INTEGER NOT NULL DEFAULT 0,
  reasoning_output_tokens INTEGER NOT NULL DEFAULT 0,
  accuracy                TEXT NOT NULL DEFAULT 'exact',
  updated_at              REAL NOT NULL,
  PRIMARY KEY (provider, session_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_sessions_path
  ON provider_sessions(provider, path);

CREATE TABLE IF NOT EXISTS daily_provider_usage (
  provider                TEXT NOT NULL,
  day                     TEXT NOT NULL,
  input_tokens            INTEGER NOT NULL DEFAULT 0,
  output_tokens           INTEGER NOT NULL DEFAULT 0,
  cached_input_tokens     INTEGER NOT NULL DEFAULT 0,
  cache_create_tokens     INTEGER NOT NULL DEFAULT 0,
  reasoning_output_tokens INTEGER NOT NULL DEFAULT 0,
  workload_tokens         INTEGER NOT NULL DEFAULT 0,
  billable_tokens         INTEGER NOT NULL DEFAULT 0,
  accuracy                TEXT NOT NULL DEFAULT 'exact',
  updated_at              REAL NOT NULL,
  PRIMARY KEY (provider, day)
);
CREATE INDEX IF NOT EXISTS idx_daily_provider_usage_day
  ON daily_provider_usage(day);
```

- [ ] **Step 4: Run schema tests**

Run:

```powershell
python -m unittest tests.test_db -v
```

Expected: all `tests.test_db` tests pass.

- [ ] **Step 5: Commit schema**

```powershell
git add token_dashboard/db.py tests/test_db.py
git commit -m "feat: add provider usage schema"
```

### Task 2: Parse and Incrementally Scan Codex Sessions

**Files:**
- Create: `token_dashboard/codex_scanner.py`
- Create: `tests/fixtures/codex_session.jsonl`
- Create: `tests/test_codex_scanner.py`

- [ ] **Step 1: Add a representative fixture**

Create `tests/fixtures/codex_session.jsonl` with a metadata row, two cumulative snapshots, and the final snapshot:

```jsonl
{"timestamp":"2026-06-06T07:00:00Z","type":"session_meta","payload":{"id":"codex-session-1"}}
{"timestamp":"2026-06-06T07:01:00Z","type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":100,"cached_input_tokens":40,"output_tokens":10,"reasoning_output_tokens":2,"total_tokens":110}}}}
{"timestamp":"2026-06-06T07:02:00Z","type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":250,"cached_input_tokens":100,"output_tokens":25,"reasoning_output_tokens":5,"total_tokens":275}}}}
```

- [ ] **Step 2: Write failing parser and scanner tests**

Create `tests/test_codex_scanner.py` covering:

```python
def test_parse_uses_final_cumulative_token_event(self):
    row = parse_codex_session(FIXTURE)
    self.assertEqual(row["session_id"], "codex-session-1")
    self.assertEqual(row["day"], "2026-06-06")
    self.assertEqual(row["input_tokens"], 250)
    self.assertEqual(row["cached_input_tokens"], 100)
    self.assertEqual(row["output_tokens"], 25)
    self.assertEqual(row["reasoning_output_tokens"], 5)

def test_parse_ignores_malformed_and_partial_lines(self):
    # Write valid metadata + malformed line + valid token event + partial EOF.
    # Assert the valid final complete event is returned.

def test_scan_replaces_changed_session_without_duplication(self):
    # Scan fixture, rewrite final cumulative event with larger totals, rescan.
    # Assert provider_sessions contains one row with the larger totals.

def test_missing_root_is_not_an_error(self):
    self.assertEqual(scan_codex_dir("/missing", self.db)["files"], 0)
```

- [ ] **Step 3: Run Codex scanner tests to verify failure**

Run:

```powershell
python -m unittest tests.test_codex_scanner -v
```

Expected: import failure because `token_dashboard.codex_scanner` does not exist.

- [ ] **Step 4: Implement the Codex scanner**

Create `token_dashboard/codex_scanner.py` with these public functions:

```python
def parse_codex_session(path: Union[str, Path]) -> Optional[dict]:
    """Return the final complete cumulative token snapshot for one session."""

def scan_codex_dir(sessions_root: Union[str, Path], db_path: Union[str, Path]) -> dict:
    """Upsert changed Codex sessions and return files/sessions counts."""
```

Parsing rules:

```python
if rec.get("type") == "session_meta":
    session_id = (rec.get("payload") or {}).get("id")

payload = rec.get("payload") or {}
if rec.get("type") == "event_msg" and payload.get("type") == "token_count":
    usage = ((payload.get("info") or {}).get("total_token_usage") or {})
    final = {
        "timestamp": rec.get("timestamp"),
        "input_tokens": int(usage.get("input_tokens") or 0),
        "cached_input_tokens": int(usage.get("cached_input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "reasoning_output_tokens": int(usage.get("reasoning_output_tokens") or 0),
    }
```

Use the filename stem as a fallback session ID. Ignore malformed lines and a
non-newline-terminated final line. Upsert by `(provider='codex', session_id)`.
Set `day` with `local_day(final["timestamp"])`. Skip files whose stored `mtime`
and `bytes_read` equal current values.

- [ ] **Step 5: Run Codex scanner tests**

Run:

```powershell
python -m unittest tests.test_codex_scanner -v
```

Expected: all Codex scanner tests pass.

- [ ] **Step 6: Commit Codex scanner**

```powershell
git add token_dashboard/codex_scanner.py tests/fixtures/codex_session.jsonl tests/test_codex_scanner.py
git commit -m "feat: scan exact Codex session usage"
```

### Task 3: Normalize and Aggregate Daily Provider Usage

**Files:**
- Create: `token_dashboard/burn.py`
- Create: `tests/test_burn.py`

- [ ] **Step 1: Write failing normalization tests**

Create `tests/test_burn.py` with:

```python
def test_claude_metric_rules(self):
    row = normalize_claude({
        "input_tokens": 10, "output_tokens": 5,
        "cache_read_tokens": 100, "cache_create_tokens": 20,
    })
    self.assertEqual(row["workload_tokens"], 135)
    self.assertEqual(row["billable_tokens"], 35)

def test_codex_metric_rules_do_not_double_count_cached_input(self):
    row = normalize_codex({
        "input_tokens": 250, "cached_input_tokens": 100,
        "output_tokens": 25, "reasoning_output_tokens": 5,
    })
    self.assertEqual(row["workload_tokens"], 280)
    self.assertEqual(row["billable_tokens"], 180)
```

Add a combined rebuild test that inserts one Claude assistant message and one
Codex `provider_sessions` row, calls `rebuild_daily_usage`, and asserts two
provider rows plus their exact workload/billable totals.

- [ ] **Step 2: Run burn tests to verify failure**

Run:

```powershell
python -m unittest tests.test_burn -v
```

Expected: import failure because `token_dashboard.burn` does not exist.

- [ ] **Step 3: Implement normalization and rebuild functions**

Create `token_dashboard/burn.py` with:

```python
def normalize_claude(row: dict) -> dict:
    cache_create = int(row.get("cache_create_tokens") or 0)
    inp = int(row.get("input_tokens") or 0)
    out = int(row.get("output_tokens") or 0)
    cached = int(row.get("cache_read_tokens") or 0)
    return {
        **row,
        "cached_input_tokens": cached,
        "cache_create_tokens": cache_create,
        "reasoning_output_tokens": 0,
        "workload_tokens": inp + out + cached + cache_create,
        "billable_tokens": inp + out + cache_create,
        "accuracy": "exact",
    }

def normalize_codex(row: dict) -> dict:
    inp = int(row.get("input_tokens") or 0)
    cached = int(row.get("cached_input_tokens") or 0)
    out = int(row.get("output_tokens") or 0)
    reasoning = int(row.get("reasoning_output_tokens") or 0)
    return {
        **row,
        "cache_create_tokens": 0,
        "workload_tokens": inp + out + reasoning,
        "billable_tokens": max(inp - cached, 0) + out + reasoning,
        "accuracy": "exact",
    }

def rebuild_daily_usage(db_path) -> dict:
    """Replace Claude and Codex daily aggregates from current source rows."""
```

Claude source query:

```sql
SELECT timestamp, input_tokens, output_tokens, cache_read_tokens,
       cache_create_5m_tokens + cache_create_1h_tokens AS cache_create_tokens
FROM messages
WHERE type='assistant' AND timestamp IS NOT NULL
```

Group the returned Claude rows in Python using `local_day(timestamp)`. This is
required because SQLite's date functions cannot reliably apply the host's
historical local timezone rules across platforms.

Codex source query:

```sql
SELECT day,
       SUM(input_tokens) AS input_tokens,
       SUM(output_tokens) AS output_tokens,
       SUM(cached_input_tokens) AS cached_input_tokens,
       SUM(reasoning_output_tokens) AS reasoning_output_tokens
FROM provider_sessions
WHERE provider='codex'
GROUP BY day
```

Delete and replace only `provider IN ('claude', 'codex')`; do not delete future
provider rows.

- [ ] **Step 4: Run burn aggregation tests**

Run:

```powershell
python -m unittest tests.test_burn -v
```

Expected: normalization and combined aggregation tests pass.

- [ ] **Step 5: Commit daily aggregation**

```powershell
git add token_dashboard/burn.py tests/test_burn.py
git commit -m "feat: aggregate daily provider usage"
```

### Task 4: Build the Burn Response Query

**Files:**
- Modify: `token_dashboard/burn.py`
- Modify: `tests/test_burn.py`

- [ ] **Step 1: Write failing response-shape and range tests**

Add tests for:

```python
def test_burn_summary_combines_providers(self):
    result = burn_summary(self.db, metric="workload")
    self.assertEqual(result["metric"], "workload")
    self.assertEqual(result["total"], 415)
    self.assertEqual(result["providers"][0]["accuracy"], "exact")
    self.assertIn("daily", result)
    self.assertIn("weekly", result)
    self.assertIn("peak_days", result)

def test_burn_summary_uses_billable_metric(self):
    result = burn_summary(self.db, metric="billable")
    self.assertEqual(result["metric"], "billable")
    self.assertEqual(result["total"], 215)

def test_burn_summary_filters_since_and_until(self):
    result = burn_summary(
        self.db, since="2026-06-06", until="2026-06-07", metric="workload"
    )
    self.assertTrue(all(d["day"] == "2026-06-06" for d in result["daily"]))

def test_invalid_metric_falls_back_to_workload(self):
    self.assertEqual(burn_summary(self.db, metric="money")["metric"], "workload")
```

- [ ] **Step 2: Run response tests to verify failure**

Run:

```powershell
python -m unittest tests.test_burn -v
```

Expected: failure because `burn_summary` is not implemented.

- [ ] **Step 3: Implement `burn_summary`**

Add:

```python
def burn_summary(db_path, since=None, until=None, metric="workload") -> dict:
    metric = metric if metric in {"workload", "billable"} else "workload"
    value_col = "workload_tokens" if metric == "workload" else "billable_tokens"
    ...
```

Return this stable shape:

```python
{
    "metric": metric,
    "total": 0,
    "day_to_date": 0,
    "peak_day": {"day": None, "tokens": 0},
    "providers": [
        {"provider": "codex", "tokens": 0, "accuracy": "exact"},
    ],
    "daily": [
        {"day": "2026-06-06", "provider": "codex", "tokens": 280},
    ],
    "weekly": [
        {"week": "2026-06-01", "tokens": 415},
    ],
    "peak_days": [
        {
            "day": "2026-06-06",
            "tokens": 415,
            "providers": {"claude": 135, "codex": 280},
        },
    ],
}
```

Build weekly totals in Python using `datetime.date.fromisoformat(day)` and
Monday-start week dates. Rank the top 10 peak days. Use local date for
`day_to_date`.

- [ ] **Step 4: Run burn tests**

Run:

```powershell
python -m unittest tests.test_burn -v
```

Expected: all Burn query tests pass.

- [ ] **Step 5: Commit Burn query**

```powershell
git add token_dashboard/burn.py tests/test_burn.py
git commit -m "feat: query multi-provider token burn"
```

### Task 5: Integrate Scanning into CLI and Server

**Files:**
- Modify: `cli.py`
- Modify: `token_dashboard/server.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_server.py`

- [ ] **Step 1: Write failing integration tests**

In `tests/test_cli.py`, make `_run` accept extra environment values and add:

```python
def test_scan_refreshes_codex_and_burn_usage(self):
    codex = os.path.join(self.tmp, "codex-sessions")
    os.makedirs(codex)
    shutil.copy(
        os.path.join(ROOT, "tests", "fixtures", "codex_session.jsonl"),
        os.path.join(codex, "session.jsonl"),
    )
    r = self._run("scan", "--projects-dir", self.proj, "--codex-sessions-dir", codex)
    self.assertEqual(r.returncode, 0, r.stderr)
    with sqlite3.connect(self.db) as c:
        providers = {row[0] for row in c.execute(
            "SELECT provider FROM daily_provider_usage"
        )}
    self.assertEqual(providers, {"claude", "codex"})
```

In `tests/test_server.py`, seed `daily_provider_usage` and add:

```python
def test_burn_json(self):
    body = json.loads(self._get("/api/burn?metric=billable"))
    self.assertEqual(body["metric"], "billable")
    self.assertIn("providers", body)

def test_burn_invalid_metric_falls_back(self):
    body = json.loads(self._get("/api/burn?metric=invalid"))
    self.assertEqual(body["metric"], "workload")
```

- [ ] **Step 2: Run integration tests to verify failure**

Run:

```powershell
python -m unittest tests.test_cli tests.test_server -v
```

Expected: failures for the unknown CLI option and missing `/api/burn`.

- [ ] **Step 3: Add one refresh coordinator**

In `cli.py`, add:

```python
def _codex_sessions(args) -> str:
    return (
        getattr(args, "codex_sessions_dir", None)
        or os.environ.get("CODEX_SESSIONS_DIR")
        or str(Path.home() / ".codex" / "sessions")
    )

def _refresh_usage(args, db):
    claude = scan_dir(_projects(args), db)
    codex = scan_codex_dir(_codex_sessions(args), db)
    burn = rebuild_daily_usage(db)
    return {"claude": claude, "codex": codex, "burn": burn}
```

Add `--codex-sessions-dir` to the common parser. Use `_refresh_usage` in
`cmd_scan` and `cmd_dashboard`.

In `token_dashboard/server.py`, pass `codex_sessions_dir` through
`build_handler`, `_scan_loop`, and `run`. After each Claude/Codex scan, call
`rebuild_daily_usage`. Add:

```python
if path == "/api/burn":
    metric = qs.get("metric", ["workload"])[0]
    return _send_json(self, burn_summary(db_path, since, until, metric))
```

Keep the current multi-root `projects_dir` work intact.

- [ ] **Step 4: Run integration tests**

Run:

```powershell
python -m unittest tests.test_cli tests.test_server -v
```

Expected: all CLI and server tests pass.

- [ ] **Step 5: Commit integration**

```powershell
git add cli.py token_dashboard/server.py tests/test_cli.py tests/test_server.py
git commit -m "feat: refresh and serve token burn data"
```

### Task 6: Add the Burn Route and Controls

**Files:**
- Create: `web/routes/burn.js`
- Modify: `web/app.js`
- Modify: `web/charts.js`

- [ ] **Step 1: Register the route**

Add to `ROUTES` in `web/app.js`:

```javascript
'/burn': () => import('/web/routes/burn.js'),
```

- [ ] **Step 2: Add a weekly trend helper**

If the existing `lineChart` cannot show compact token labels cleanly, add this
optional formatter argument in `web/charts.js`:

```javascript
export function lineChart(el, { x, series, yFormatter }) {
  ...
  yAxis: {
    ...Y_AXIS,
    type: 'value',
    axisLabel: { ...Y_AXIS.axisLabel, formatter: yFormatter },
  },
}
```

Preserve existing callers when `yFormatter` is undefined.

- [ ] **Step 3: Implement Burn route state and API loading**

Create `web/routes/burn.js` with:

```javascript
import { api, fmt } from '/web/app.js';
import { lineChart } from '/web/charts.js';

const RANGES = [
  { key: '90d', label: '90d', days: 90 },
  { key: '180d', label: '180d', days: 180 },
  { key: '1y', label: '1y', days: 365 },
  { key: 'all', label: 'All', days: null },
];

const METRICS = [
  { key: 'workload', label: 'Workload' },
  { key: 'billable', label: 'Billable' },
];
```

Read `range` and `metric` from hash query parameters, defaulting to `1y` and
`workload`. Fetch `/api/burn` with matching query parameters. Render:

- Page title and exact/estimated explanation
- Range buttons and metric toggle
- Total, day-to-date, and peak-day cards
- `#burn-weekly`
- `#burn-heatmaps`
- Peak-day table

- [ ] **Step 4: Implement heatmap lane rendering**

Use a CSS-grid-friendly DOM shape:

```html
<section class="burn-lane">
  <header>
    <strong>Codex</strong>
    <span>8.7B</span>
    <span class="badge">exact</span>
  </header>
  <div class="burn-grid">
    <button class="burn-cell level-3"
      title="2026-06-06 · Codex · 280 tokens"></button>
  </div>
</section>
```

Build a complete contiguous day list for the selected range. Render Total
first, then providers sorted by total descending. Assign levels using
logarithmic bins:

```javascript
function heatLevel(value, max) {
  if (!value || !max) return 0;
  return Math.max(1, Math.min(5,
    Math.ceil((Math.log10(value + 1) / Math.log10(max + 1)) * 5)));
}
```

- [ ] **Step 5: Verify JavaScript syntax**

Run:

```powershell
node --check web/routes/burn.js
node --check web/app.js
node --check web/charts.js
```

Expected: each command exits `0` with no output.

- [ ] **Step 6: Commit Burn route**

```powershell
git add web/routes/burn.js web/app.js web/charts.js
git commit -m "feat: add token burn route"
```

### Task 7: Style and Visually Verify the Burn Page

**Files:**
- Modify: `web/style.css`

- [ ] **Step 1: Add responsive Burn styles**

Add focused styles:

```css
.burn-toolbar { display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
.burn-summary { grid-template-columns:repeat(3,minmax(0,1fr)); }
.burn-lane { display:grid; grid-template-columns:140px minmax(0,1fr); gap:16px; padding:14px 0; border-top:1px solid var(--border); }
.burn-lane header { display:flex; flex-direction:column; gap:4px; }
.burn-grid { display:grid; grid-auto-flow:column; grid-template-rows:repeat(7,12px); grid-auto-columns:12px; gap:4px; overflow-x:auto; padding-bottom:6px; }
.burn-cell { width:12px; height:12px; padding:0; border-radius:3px; border:1px solid var(--border); background:var(--panel-2); }
.burn-cell.level-1 { background:rgba(63,182,139,.20); }
.burn-cell.level-2 { background:rgba(63,182,139,.36); }
.burn-cell.level-3 { background:rgba(63,182,139,.55); }
.burn-cell.level-4 { background:rgba(63,182,139,.75); }
.burn-cell.level-5 { background:var(--good); }
@media (max-width:720px) {
  .burn-summary { grid-template-columns:1fr; }
  .burn-lane { grid-template-columns:1fr; }
}
```

Merge with the user's current `web/style.css`; do not remove the existing MAI
badge or other uncommitted styles.

- [ ] **Step 2: Start the dashboard without opening an external browser**

Run:

```powershell
python cli.py dashboard --no-open
```

Expected: server reports `Token Dashboard listening on http://127.0.0.1:8080/`.

- [ ] **Step 3: Verify API data**

Run:

```powershell
Invoke-RestMethod 'http://127.0.0.1:8080/api/burn?metric=workload'
Invoke-RestMethod 'http://127.0.0.1:8080/api/burn?metric=billable'
```

Expected: both return provider, daily, weekly, and peak-day data; metric and
totals differ when cached input exists.

- [ ] **Step 4: Visually verify with the Browser plugin**

Open `http://127.0.0.1:8080/#/burn?range=1y&metric=workload` using the in-app
Browser. Verify:

- Burn appears in top navigation.
- Workload and Billable toggles refresh values.
- Total lane equals provider-lane sums.
- Heatmap lanes align and remain horizontally scrollable.
- Empty days are visible but subdued.
- Mobile/narrow layout remains readable.
- Existing Overview still renders.

- [ ] **Step 5: Commit styling**

```powershell
git add web/style.css
git commit -m "style: add responsive token burn heatmap"
```

### Task 8: Document Behavior and Limitations

**Files:**
- Modify: `README.md`
- Modify: `docs/KNOWN_LIMITATIONS.md`

- [ ] **Step 1: Update README**

Add the Burn tab to the feature list and document:

```text
Codex source: ~/.codex/sessions/**/*.jsonl
Override: --codex-sessions-dir or CODEX_SESSIONS_DIR
Workload: total model processing, including Claude cache reads
Billable: provider-normalized usage excluding cache reads/cached input
```

State that all ingestion remains local and that Burn does not expose prompt
text.

- [ ] **Step 2: Document limitations**

Add to `docs/KNOWN_LIMITATIONS.md`:

- Codex totals use the final cumulative token event per session.
- A Codex session spanning midnight is attributed to the final event's local
  day rather than split across days.
- Billable is a normalized token comparison, not a universal cost estimate.
- ChatGPT ingestion is not yet implemented.

- [ ] **Step 3: Commit documentation**

```powershell
git add README.md docs/KNOWN_LIMITATIONS.md
git commit -m "docs: explain multi-provider token burn"
```

### Task 9: Full Verification

**Files:**
- Verify all changed files

- [ ] **Step 1: Run focused tests**

Run:

```powershell
python -m unittest tests.test_db tests.test_codex_scanner tests.test_burn tests.test_cli tests.test_server -v
```

Expected: all focused tests pass.

- [ ] **Step 2: Run complete test suite**

Run:

```powershell
python -m unittest discover tests
```

Expected: all tests pass with no failures or errors.

- [ ] **Step 3: Run syntax and whitespace checks**

Run:

```powershell
node --check web/routes/burn.js
node --check web/app.js
node --check web/charts.js
git diff --check
```

Expected: all commands exit `0`.

- [ ] **Step 4: Verify current repository state**

Run:

```powershell
git status --short --branch
git log --oneline -10
```

Expected: implementation commits are present. Any pre-existing unrelated
changes remain intact and are reported explicitly; no user changes were
reverted.

- [ ] **Step 5: Final manual acceptance check**

Start the dashboard, open the Burn tab, switch every range and metric, then
visit Overview, Sessions, and Settings. Confirm no existing page regressed and
the Burn totals match `/api/burn`.
