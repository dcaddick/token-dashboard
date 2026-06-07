# Exact Model Token Burn Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an exact Providers/Models grouping toggle to Burn using Claude message models and Codex cumulative token-event deltas.

**Architecture:** Preserve provider-level Burn totals while storing exact source contributions at `(provider, model, day)` granularity. Claude contributions rebuild from assistant messages; Codex scanning atomically replaces per-session model/day delta contributions. The Burn API aggregates the same normalized rows into provider or model lanes.

**Tech Stack:** Python 3 stdlib, SQLite, `unittest`, vanilla JavaScript, CSS Grid, ECharts.

---

## File Structure

- Modify `token_dashboard/db.py`: migrate daily provider usage to a model-aware key and add Codex model/day contribution storage.
- Modify `token_dashboard/codex_scanner.py`: parse cumulative token events into active-model deltas and atomically replace session contributions.
- Modify `token_dashboard/burn.py`: rebuild Claude model rows, roll up Codex contribution rows, and return provider/model lane summaries.
- Modify `token_dashboard/server.py`: accept and validate the Burn `group` query parameter and include model-aware rows in refresh snapshots.
- Modify `web/routes/burn.js`: add Providers/Models toggle and render neutral lane data.
- Modify `web/app.js`: add a reusable exact model-label formatter if Burn and existing model views need the same naming.
- Modify `tests/test_db.py`, `tests/test_codex_scanner.py`, `tests/test_burn.py`, and `tests/test_server.py`: migration, attribution, grouping, and integration coverage.
- Modify `README.md` and `docs/KNOWN_LIMITATIONS.md`: document exact model lanes and attribution boundaries.

The unrelated untracked `.vscode/` directory must remain untouched.

## Stable Data Contracts

Use these exact unknown model labels:

```python
UNKNOWN_MODELS = {
    "claude": "unknown-claude",
    "codex": "unknown-codex",
}
```

Daily aggregate uniqueness:

```text
provider, model, day
```

Codex contribution uniqueness:

```text
session_id, model, day
```

Burn response lane shape:

```python
{
    "key": "model:claude:claude-opus-4-7",
    "label": "Claude Opus 4.7",
    "provider": "claude",
    "model": "claude-opus-4-7",
    "tokens": 123,
    "accuracy": "exact",
}
```

Provider keys use `provider:<provider>` and model keys use
`model:<provider>:<model>`. This prevents identical model names from different
providers colliding.

### Task 1: Add Model-Aware Usage Schema and Migration

**Files:**
- Modify: `token_dashboard/db.py`
- Modify: `tests/test_db.py`

- [ ] **Step 1: Write failing schema and migration tests**

Update expected tables to include `codex_model_usage`. Add:

```python
def test_daily_provider_usage_is_unique_by_provider_model_day(self):
    init_db(self.db_path)
    with sqlite3.connect(self.db_path) as c:
        cols = {row[1] for row in c.execute(
            "PRAGMA table_info(daily_provider_usage)"
        )}
        self.assertIn("model", cols)
        c.execute("""
          INSERT INTO daily_provider_usage
            (provider, model, day, workload_tokens, billable_tokens,
             accuracy, updated_at)
          VALUES ('claude','model-a','2026-06-07',1,1,'exact',1)
        """)
        c.execute("""
          INSERT INTO daily_provider_usage
            (provider, model, day, workload_tokens, billable_tokens,
             accuracy, updated_at)
          VALUES ('claude','model-b','2026-06-07',2,2,'exact',1)
        """)
        with self.assertRaises(sqlite3.IntegrityError):
            c.execute("""
              INSERT INTO daily_provider_usage
                (provider, model, day, workload_tokens, billable_tokens,
                 accuracy, updated_at)
              VALUES ('claude','model-a','2026-06-07',3,3,'exact',1)
            """)
```

Add a migration test that creates the current pre-model table, inserts provider
rows, calls `init_db`, and asserts the rows survive with:

```text
claude -> unknown-claude
codex -> unknown-codex
other provider -> unknown-<provider>
```

- [ ] **Step 2: Run database tests to verify failure**

Run:

```powershell
python -m unittest tests.test_db -v
```

Expected: failures because model-aware tables and migration do not exist.

- [ ] **Step 3: Add schema and migration**

Add:

```sql
CREATE TABLE IF NOT EXISTS codex_model_usage (
  session_id              TEXT NOT NULL,
  model                   TEXT NOT NULL,
  day                     TEXT NOT NULL,
  input_tokens            INTEGER NOT NULL DEFAULT 0,
  output_tokens           INTEGER NOT NULL DEFAULT 0,
  cached_input_tokens     INTEGER NOT NULL DEFAULT 0,
  cache_create_tokens     INTEGER NOT NULL DEFAULT 0,
  reasoning_output_tokens INTEGER NOT NULL DEFAULT 0,
  accuracy                TEXT NOT NULL DEFAULT 'exact',
  updated_at              REAL NOT NULL,
  PRIMARY KEY (session_id, model, day)
);
CREATE INDEX IF NOT EXISTS idx_codex_model_usage_day
  ON codex_model_usage(day);
```

Make `daily_provider_usage.model TEXT NOT NULL` and use:

```sql
PRIMARY KEY (provider, model, day)
```

Implement `_migrate_daily_usage_add_model(conn)` before `executescript(SCHEMA)`.
If the existing table lacks `model`, rename it, create the new table, copy rows
with deterministic unknown labels, then drop the old table. Perform all steps
inside one SQLite transaction.

- [ ] **Step 4: Run database tests**

Run:

```powershell
python -m unittest tests.test_db -v
```

Expected: all database tests pass.

- [ ] **Step 5: Commit schema migration**

```powershell
git add token_dashboard/db.py tests/test_db.py
git commit -m "feat: add model-aware burn schema"
```

### Task 2: Parse Exact Codex Model Deltas

**Files:**
- Modify: `token_dashboard/codex_scanner.py`
- Modify: `tests/test_codex_scanner.py`

- [ ] **Step 1: Add test helpers and failing delta tests**

Add:

```python
def _turn_context(timestamp, model):
    return {
        "timestamp": timestamp,
        "type": "turn_context",
        "payload": {"model": model},
    }
```

Add tests:

```python
def test_parse_attributes_cumulative_deltas_to_active_models(self):
    # context model-a; cumulative 100/40/10/2
    # context model-b; cumulative 250/100/25/5
    # expect model-a delta 100/40/10/2
    # expect model-b delta 150/60/15/3

def test_parse_splits_deltas_across_local_days(self):
    # Same model, token events on two local dates produce two contributions.

def test_parse_clamps_negative_reset_deltas_to_zero(self):
    # A lower cumulative snapshot produces a zero delta, never negative usage.

def test_parse_uses_unknown_codex_before_first_model_context(self):
    # First token event precedes turn_context; contribution model is unknown-codex.
```

- [ ] **Step 2: Run parser tests to verify failure**

Run:

```powershell
python -m unittest tests.test_codex_scanner.ParseCodexSessionTests -v
```

Expected: failures because parsed model contributions are absent.

- [ ] **Step 3: Extend parser return contract**

Change `_parse_complete_lines` to return:

```python
{
    "session": {
        "session_id": "...",
        "day": "...",
        "input_tokens": ...,
        "cached_input_tokens": ...,
        "output_tokens": ...,
        "reasoning_output_tokens": ...,
    },
    "contributions": [
        {
            "model": "model-a",
            "day": "2026-06-07",
            "input_tokens": 100,
            "cached_input_tokens": 40,
            "output_tokens": 10,
            "reasoning_output_tokens": 2,
        },
    ],
}
```

Keep `parse_codex_session(path)` backward-compatible by returning only
`parsed["session"]`. Add:

```python
def parse_codex_model_usage(path) -> list[dict]:
    parsed, _ = _parse_complete_lines(Path(path))
    return parsed["contributions"] if parsed else []
```

Delta calculation:

```python
def _usage_delta(current, previous):
    return {
        key: max(_token_count(current.get(key)) - _token_count(previous.get(key)), 0)
        for key in TOKEN_KEYS
    }
```

Track active model from complete `turn_context` records and aggregate deltas by
`(model or "unknown-codex", local_day(timestamp))`.

- [ ] **Step 4: Run parser tests**

Run:

```powershell
python -m unittest tests.test_codex_scanner.ParseCodexSessionTests -v
```

Expected: parser tests pass, including existing final cumulative behavior.

- [ ] **Step 5: Commit Codex delta parser**

```powershell
git add token_dashboard/codex_scanner.py tests/test_codex_scanner.py
git commit -m "feat: attribute Codex token deltas by model"
```

### Task 3: Persist and Replace Codex Model Contributions

**Files:**
- Modify: `token_dashboard/codex_scanner.py`
- Modify: `tests/test_codex_scanner.py`

- [ ] **Step 1: Write failing lifecycle tests**

Add tests proving:

```python
def test_scan_stores_model_contributions(self):
    # Scan a model-switch session and assert codex_model_usage rows.

def test_changed_session_replaces_old_model_contributions(self):
    # Rewrite session from model-a to model-b; no stale model-a row survives.

def test_deleted_session_removes_model_contributions(self):
    # Delete canonical source; provider_sessions and codex_model_usage clear.

def test_duplicate_session_uses_only_canonical_contributions(self):
    # Duplicate files with same session ID do not double-count contributions.

def test_promoted_duplicate_replaces_contributions(self):
    # Delete winner; promoted file's contribution set replaces prior winner.
```

- [ ] **Step 2: Run scan tests to verify failure**

Run:

```powershell
python -m unittest tests.test_codex_scanner.ScanCodexDirTests -v
```

Expected: failures because `codex_model_usage` is not maintained.

- [ ] **Step 3: Add atomic contribution replacement**

Add:

```python
def _replace_model_usage(conn, session_id: str, contributions: list[dict]) -> None:
    conn.execute("DELETE FROM codex_model_usage WHERE session_id=?", (session_id,))
    conn.executemany(
        """
        INSERT INTO codex_model_usage (
          session_id, model, day, input_tokens, output_tokens,
          cached_input_tokens, reasoning_output_tokens, accuracy, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'exact', ?)
        """,
        ...
    )
```

Call replacement only for the canonical accepted duplicate. Whenever a
provider session is deleted, invalidated, or replaced, delete its contribution
rows in the same transaction.

- [ ] **Step 4: Run scanner tests**

Run:

```powershell
python -m unittest tests.test_codex_scanner tests.test_db -v
```

Expected: all scanner and database tests pass.

- [ ] **Step 5: Commit contribution lifecycle**

```powershell
git add token_dashboard/codex_scanner.py tests/test_codex_scanner.py
git commit -m "feat: persist Codex model usage contributions"
```

### Task 4: Rebuild Exact Daily Usage by Model

**Files:**
- Modify: `token_dashboard/burn.py`
- Modify: `tests/test_burn.py`

- [ ] **Step 1: Update fixtures and write failing model rebuild tests**

Update `_insert_message` to accept and insert `model`. Update `_insert_daily`
to accept a model value.

Add:

```python
def test_rebuild_splits_claude_usage_by_model(self):
    # Same day, two assistant models -> two Claude daily rows.

def test_rebuild_preserves_mai_model_identity(self):
    # mai-* model remains its exact ID.

def test_rebuild_uses_unknown_claude_for_missing_model(self):
    # NULL message.model -> unknown-claude.

def test_rebuild_rolls_up_codex_model_contributions(self):
    # codex_model_usage rows become exact daily_provider_usage rows.

def test_provider_totals_equal_sum_of_model_rows(self):
    # Summing daily model rows reproduces pre-model provider totals.
```

- [ ] **Step 2: Run Burn rebuild tests to verify failure**

Run:

```powershell
python -m unittest tests.test_burn -v
```

Expected: failures because daily usage still groups only by provider/day.

- [ ] **Step 3: Rebuild by provider/model/day**

Add:

```python
def normalized_model(provider: str, model) -> str:
    value = str(model or "").strip()
    return value or f"unknown-{provider}"
```

Group Claude rows by `(local_day(timestamp), normalized_model("claude", model))`.
Read Codex rows from `codex_model_usage`, not `provider_sessions`.

Delete/reinsert only Claude and Codex rows, preserving future providers and
their model rows.

Return counts as model/day rows:

```python
{"claude": <rows>, "codex": <rows>}
```

- [ ] **Step 4: Run Burn tests**

Run:

```powershell
python -m unittest tests.test_burn -v
```

Expected: all normalization and rebuild tests pass.

- [ ] **Step 5: Commit model-aware rebuild**

```powershell
git add token_dashboard/burn.py tests/test_burn.py
git commit -m "feat: rebuild daily burn usage by model"
```

### Task 5: Return Neutral Provider or Model Lanes

**Files:**
- Modify: `token_dashboard/burn.py`
- Modify: `tests/test_burn.py`

- [ ] **Step 1: Write failing grouping tests**

Add:

```python
def test_provider_group_returns_provider_lanes(self):
    result = burn_summary(self.db, group="provider")
    self.assertEqual(result["group"], "provider")
    self.assertEqual(result["lanes"][0]["key"], "provider:codex")

def test_model_group_returns_model_lanes(self):
    result = burn_summary(self.db, group="model")
    self.assertEqual(result["group"], "model")
    self.assertEqual(result["lanes"][0]["key"], "model:codex:gpt-5")

def test_invalid_group_falls_back_to_provider(self):
    self.assertEqual(burn_summary(self.db, group="invalid")["group"], "provider")

def test_grouping_does_not_change_combined_totals(self):
    provider = burn_summary(self.db, group="provider")
    model = burn_summary(self.db, group="model")
    self.assertEqual(provider["total"], model["total"])
    self.assertEqual(provider["weekly"], model["weekly"])
```

- [ ] **Step 2: Run grouping tests to verify failure**

Run:

```powershell
python -m unittest tests.test_burn -v
```

Expected: failures because `burn_summary` has no group/lane support.

- [ ] **Step 3: Implement neutral lanes**

Change signature:

```python
def burn_summary(db_path, since=None, until=None, metric="workload", group="provider"):
```

Validate:

```python
group = group if group in {"provider", "model"} else "provider"
```

Always return:

```python
{
    "metric": metric,
    "group": group,
    "total": ...,
    "day_to_date": ...,
    "peak_day": ...,
    "lanes": [...],
    "daily": [{"day": ..., "lane": ..., "tokens": ...}],
    "weekly": [...],
    "peak_days": [
        {"day": ..., "tokens": ..., "lanes": {lane_key: tokens}}
    ],
}
```

Provider labels are title-cased provider names. Model labels can initially use
the exact stored ID; frontend formatting is handled in Task 7. Lane ordering is
descending token count then key.

- [ ] **Step 4: Run Burn tests**

Run:

```powershell
python -m unittest tests.test_burn -v
```

Expected: all Burn grouping tests pass.

- [ ] **Step 5: Commit lane query**

```powershell
git add token_dashboard/burn.py tests/test_burn.py
git commit -m "feat: group token burn by provider or model"
```

### Task 6: Integrate Burn Grouping into Server Refresh and API

**Files:**
- Modify: `token_dashboard/server.py`
- Modify: `tests/test_server.py`

- [ ] **Step 1: Write failing API and refresh tests**

Add:

```python
def test_burn_model_group_json(self):
    body = json.loads(self._get("/api/burn?group=model"))
    self.assertEqual(body["group"], "model")
    self.assertIn("lanes", body)

def test_burn_invalid_group_falls_back(self):
    body = json.loads(self._get("/api/burn?group=invalid"))
    self.assertEqual(body["group"], "provider")

def test_usage_snapshot_detects_model_only_changes(self):
    # Same provider/day/provider total, changed model split -> snapshot differs.
```

- [ ] **Step 2: Run server tests to verify failure**

Run:

```powershell
python -m unittest tests.test_server -v
```

Expected: failures for missing group support and model-aware snapshot.

- [ ] **Step 3: Pass group and model-aware snapshots**

Update `_usage_snapshot` query to include `model` and order by
`provider, model, day`.

Update `/api/burn`:

```python
group = qs.get("group", ["provider"])[0]
return _send_json(
    self,
    burn_summary(db_path, since, until, metric=metric, group=group),
)
```

- [ ] **Step 4: Run server tests**

Run:

```powershell
python -m unittest tests.test_server -v
```

Expected: all server tests pass.

- [ ] **Step 5: Commit API grouping**

```powershell
git add token_dashboard/server.py tests/test_server.py
git commit -m "feat: expose model-grouped token burn"
```

### Task 7: Add Providers/Models Burn Toggle

**Files:**
- Modify: `web/routes/burn.js`
- Modify: `web/app.js`
- Modify: `web/style.css`

- [ ] **Step 1: Add neutral grouping state**

In `web/routes/burn.js` add:

```javascript
const GROUPS = [
  { key: 'provider', label: 'Providers' },
  { key: 'model', label: 'Models' },
];
```

Update `readState` and `writeState` to preserve `range`, `metric`, and `group`.
Default to provider.

- [ ] **Step 2: Add exact model label formatting**

Add to `fmt` in `web/app.js`:

```javascript
burnModel: (provider, model) => {
  if (model === `unknown-${provider}`) return `Unknown ${provider.charAt(0).toUpperCase() + provider.slice(1)}`;
  return fmt.modelShort(model);
},
```

Ensure MAI IDs retain an MAI prefix and unknown labels are readable.

- [ ] **Step 3: Render neutral lane response**

Replace assumptions about `summary.providers` and `row.provider` with:

```javascript
summary.lanes
row.lane
item.lanes
```

Render Total first, then API lanes. For model lanes show provider context:

```html
<small>Codex · exact local usage</small>
```

Add group controls before metric/range controls and include `group` in the API
query.

- [ ] **Step 4: Verify frontend syntax**

Run:

```powershell
node --check web/routes/burn.js
node --check web/app.js
```

Expected: both commands exit `0`.

- [ ] **Step 5: Commit frontend grouping**

```powershell
git add web/routes/burn.js web/app.js web/style.css
git commit -m "feat: toggle Burn lanes by provider or model"
```

### Task 8: Document Exact Model Attribution

**Files:**
- Modify: `README.md`
- Modify: `docs/KNOWN_LIMITATIONS.md`

- [ ] **Step 1: Update README**

Document:

- Burn Providers/Models grouping toggle
- Exact Claude, MAI, and Codex model attribution
- Unknown model lanes preserve unattributed exact usage
- Gemini and ChatGPT remain deferred

- [ ] **Step 2: Update limitations**

Document:

- Codex model usage uses deltas between cumulative snapshots.
- A token event is assigned to the active model from the latest complete
  `turn_context`.
- Resets are clamped to zero.
- Provider totals should equal summed model totals, but unknown model lanes can
  appear when model context is missing.

- [ ] **Step 3: Commit documentation**

```powershell
git add README.md docs/KNOWN_LIMITATIONS.md
git commit -m "docs: explain exact model Burn attribution"
```

### Task 9: Full Verification and Live Migration Check

**Files:**
- Verify all changed files

- [ ] **Step 1: Run focused tests**

Run:

```powershell
python -m unittest tests.test_db tests.test_codex_scanner tests.test_burn tests.test_server -v
```

Expected: all focused tests pass.

- [ ] **Step 2: Run full suite**

Run:

```powershell
python -m unittest discover tests
```

Expected: all tests pass.

- [ ] **Step 3: Run frontend and whitespace checks**

Run:

```powershell
node --check web/routes/burn.js
node --check web/app.js
git diff --check
```

Expected: all commands exit `0`.

- [ ] **Step 4: Verify migration preserves provider totals**

Before restarting against the live DB, record:

```powershell
Invoke-RestMethod 'http://127.0.0.1:8080/api/burn?group=provider&metric=workload'
Invoke-RestMethod 'http://127.0.0.1:8080/api/burn?group=provider&metric=billable'
```

Restart the dashboard on the new code and query the same endpoints. Provider
totals must remain equal before and after migration.

- [ ] **Step 5: Verify model grouping live**

Query:

```powershell
Invoke-RestMethod 'http://127.0.0.1:8080/api/burn?group=model&metric=workload'
Invoke-RestMethod 'http://127.0.0.1:8080/api/burn?group=model&metric=billable'
```

Confirm:

- Claude and Codex model lanes exist.
- MAI lanes exist when present in Claude transcripts.
- Unknown lanes are visible if needed.
- Sum of model lane tokens equals the provider-mode total.

- [ ] **Step 6: Visually verify Burn**

Open:

```text
http://127.0.0.1:8080/#/burn?group=model&range=1y&metric=workload
```

Verify Providers/Models switching, Workload/Billable switching, lane labels,
heatmaps, peak-day contributions, mobile controls, and existing provider mode.

- [ ] **Step 7: Verify repository state**

Run:

```powershell
git status --short --branch
git log --oneline -12
```

Expected: implementation commits are present; `.vscode/` remains untouched.
