# Exact Model Token Burn Design

## Goal

Extend the Burn tab so exact local token usage can be viewed by model as well
as by provider.

The first phase uses only exact local counters already available from Claude
Code and Codex. Gemini CLI and estimated ChatGPT usage are deferred.

## User Experience

Add a grouping toggle to the Burn tab:

- **Providers**: Total, Claude, and Codex lanes
- **Models**: Total, followed by exact model lanes sorted by selected-metric
  token usage

The existing range and Workload/Billable controls remain unchanged.

Model lanes use concise labels where possible:

- Claude Opus, Sonnet, and Haiku model IDs are shortened for readability.
- MAI model IDs remain clearly labeled as MAI.
- Codex/OpenAI model IDs use their recorded model name.
- Missing model identities remain visible as `Unknown Claude` or
  `Unknown Codex`; usage is never silently discarded.

Provider mode remains the default so the existing Burn view does not change
unexpectedly.

## Data Model

Add a nullable `model` dimension to provider session and daily usage data.

Daily rows become unique by:

```text
provider, model, day
```

`model` is stored as a non-null normalized value in daily aggregates, using an
explicit unknown-provider label when source data lacks model identity.

The model dimension does not replace provider. Every row retains both so the
API can aggregate either grouping without rescanning source logs.

## Claude Attribution

Claude Code assistant messages already include exact usage counters and
`message.model`.

During daily rebuild:

1. Convert each assistant timestamp to the machine's local calendar day.
2. Normalize the model identifier.
3. Aggregate exact token buckets by `(provider='claude', model, day)`.
4. Apply the existing Claude Workload and Billable formulas.

MAI models present in Claude-compatible transcripts are grouped under their
exact recorded model ID, with provider remaining `claude` because that is the
source transcript format.

## Codex Attribution

Codex emits cumulative session token snapshots. Assigning an entire session to
its final model would be wrong when a session changes model, so model-level
attribution uses token-event deltas.

For each Codex session:

1. Read complete JSONL records in timestamp order.
2. Track the active model from `turn_context.payload.model`.
3. For each cumulative `token_count` event, subtract the previous cumulative
   snapshot bucket-by-bucket.
4. Clamp negative deltas to zero because resets or malformed snapshots must not
   create negative usage.
5. Attribute the delta to the active model at that event.
6. Attribute the delta to the token event's local calendar day.
7. If no model is active, use `Unknown Codex`.

The provider-level Codex totals must equal the sum of all Codex model deltas.
The existing final cumulative session totals remain useful for lifecycle and
deduplication checks, but daily/model Burn rows are built from deltas.

## Storage and Refresh

Store exact Codex model/day contributions in a dedicated source table so a
changed session can replace all of its prior contributions atomically.

Suggested uniqueness:

```text
provider, session_id, model, day
```

On a changed, deleted, moved, or duplicate Codex session:

- Remove the prior session contributions.
- Parse and insert the canonical session's current contributions.
- Rebuild affected daily aggregates.

Claude model/day rows continue to rebuild from the existing messages table.
Future provider/model rows must remain untouched.

## API

Extend:

```text
GET /api/burn?group=provider|model&metric=workload|billable
```

`group` defaults to `provider`. Invalid values fall back to `provider`.

The existing response shape remains stable. In model mode:

- `providers` is replaced by a new neutral `lanes` collection.
- Each lane contains:
  - `key`
  - `label`
  - `provider`
  - `model`
  - `tokens`
  - `accuracy`
- Daily rows include the same lane key.
- Peak-day contributions use lane keys.

To avoid ambiguous behavior, provider mode also uses `lanes`; the frontend no
longer assumes every lane is a provider.

## Frontend

Add a Providers/Models toggle beside the existing metric and date controls.

The Burn route:

- Stores `group` in hash query parameters.
- Requests matching API aggregation.
- Renders Total first, then lanes sorted by selected metric.
- Shows provider context under model labels where useful.
- Preserves exact/estimated labels.
- Escapes all source-provided labels.

The weekly trend and summary cards remain combined totals and do not change
when grouping switches.

## Error Handling

- Missing Claude model identifiers become `Unknown Claude`.
- Missing Codex active models become `Unknown Codex`.
- Invalid or negative Codex cumulative deltas are clamped to zero.
- Malformed and partial Codex lines remain ignored and retried using current
  scanner behavior.
- A model attribution failure cannot prevent provider totals from refreshing.

## Testing

Add focused tests for:

- Claude daily aggregation split by model
- MAI model identity preservation
- Unknown Claude fallback
- Codex model changes within one session
- Codex cumulative delta calculations
- Delta attribution across local-day boundaries
- Negative/reset deltas clamped to zero
- Unknown Codex fallback
- Changed/deleted/duplicate session contribution replacement
- Provider totals equaling summed model totals
- API provider/model grouping and invalid-group fallback
- Frontend group state and empty model lanes

Run the complete test suite and verify that existing provider-mode totals remain
unchanged after migration.

## Deferred

- Gemini CLI ingestion
- ChatGPT consumer estimates
- Cost comparisons by model
- Model-level Prompts, Sessions, Projects, Skills, or Tips views
