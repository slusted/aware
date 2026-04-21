# Spec 01 — User Signal Log

**Status:** Draft
**Owner:** Simon
**Depends on:** nothing
**Unblocks:** 02 (preference rollup), 03 (ranker), 04 (preference chat)

## Purpose

Capture an append-only, per-user event stream of interactions with findings. This is the *only* input to the preference rollup (spec 02) and the audit trail for explainability (spec 03). Everything downstream is derivable from this log — so the schema and event taxonomy decided here constrain the entire ranker.

## Non-goals

- Computing preferences (spec 02).
- Scoring / ranking (spec 03).
- The chat UX for editing taste (spec 04).
- Replacing `SignalView`. Read-state UI (unread badge, dismissed filter) keeps using `SignalView` as-is; the signal log is additive.

## Design principles

1. **Facts, not interpretations.** Store what happened (`dismiss`, `dwell_ms=4200`) — never a precomputed weight. Weights live in the rollup so they can be retuned without backfilling.
2. **Append-only.** No updates, no deletes (except retention). Every row is immutable.
3. **One table, one shape.** All event types share a row; type-specific data goes in `meta` JSON. Keeps queries simple and future event types cheap.
4. **Decoupled from `SignalView`.** Pin/dismiss/snooze continue to write `SignalView`; they *additionally* append an event row in the same transaction. The log can be rebuilt independently; `SignalView` is a materialized view of the latest pin/dismiss/snooze state.
5. **Explainable by construction.** Every ranker decision must be traceable back to specific event rows ("you dismissed 3 hiring-signal findings for Indeed in the last 14 days").

## Schema

New table: `user_signal_events`.

| column        | type       | null | notes                                                                      |
| ------------- | ---------- | ---- | -------------------------------------------------------------------------- |
| `id`          | integer pk | no   |                                                                            |
| `user_id`     | fk users   | no   | `ON DELETE CASCADE`                                                        |
| `finding_id`  | fk findings| yes  | NULL for non-finding events (e.g. `chat_pref_update`). `ON DELETE SET NULL`|
| `event_type`  | string(32) | no   | See taxonomy below. Validated at insert time.                              |
| `value`       | float      | yes  | Event-specific magnitude. Raw data only (e.g. `dwell_ms`), never a weight. |
| `source`      | string(16) | no   | UI surface: `stream` \| `detail` \| `email` \| `chat` \| `onboarding` \| `system` |
| `meta`        | json       | no   | Default `{}`. Freeform per event type. See taxonomy.                       |
| `ts`          | datetime   | no   | Default `utcnow`. Index.                                                   |

Indexes:
- `(user_id, ts DESC)` — primary access pattern (latest events for user)
- `(user_id, event_type, ts DESC)` — rollup reads per-type slices
- `(finding_id)` — reverse lookup ("who reacted to this finding")

No uniqueness constraints. De-dup is the client's job (see emission rules).

## Event taxonomy

Each event is one of the following. `event_type` is a closed enum — adding a new type requires a spec amendment.

### Implicit (client-emitted unless noted)

| type        | fires when                                                                          | `value`        | `meta` example                         |
| ----------- | ----------------------------------------------------------------------------------- | -------------- | -------------------------------------- |
| `shown`     | Server renders a card in a stream response. Batched insert after response sent.     | null           | `{"position": 7, "filter_id": 12}`     |
| `view`      | Card is ≥50% in viewport for ≥500ms. IntersectionObserver.                          | null           | `{"position": 7}`                      |
| `dwell`     | Card leaves viewport or user navigates; one-shot per view session.                  | `dwell_ms`     | `{"position": 7}`                      |
| `open`      | User clicks through to the source URL or expands the card for full content.         | null           | `{"target": "url" \| "expand"}`        |
| `pin`       | User pins. Server-emitted alongside `SignalView` write.                             | null           | `{}`                                   |
| `dismiss`   | User dismisses. Server-emitted alongside `SignalView` write.                        | null           | `{}`                                   |
| `snooze`    | User snoozes. Server-emitted alongside `SignalView` write.                          | null           | `{"snoozed_until": "2026-05-01T..."}`  |
| `unpin`     | User removes a pin.                                                                 | null           | `{}`                                   |
| `undismiss` | User restores a dismissed finding.                                                  | null           | `{}`                                   |

### Explicit (user ratings)

| type              | fires when                               | `value` | `meta`                                  |
| ----------------- | ---------------------------------------- | ------- | --------------------------------------- |
| `rate_up`         | Thumbs-up on a card.                     | null    | `{}`                                    |
| `rate_down`       | Thumbs-down on a card.                   | null    | `{}`                                    |
| `more_like_this`  | "More like this" action.                 | null    | `{"reason_tags": ["hiring","series-b"]}` (optional) |
| `less_like_this`  | "Less like this" action.                 | null    | `{"reason_tags": ["price-change"]}` (optional) |

### System

| type                | fires when                                       | `value` | `meta`                                                      |
| ------------------- | ------------------------------------------------ | ------- | ----------------------------------------------------------- |
| `chat_pref_update`  | Preference chat (spec 04) commits a taste-doc diff. | null | `{"diff_summary": "...", "chat_turn_id": 42}`               |
| `onboarding_seed`   | First-login onboarding pre-seeds taste.          | null    | `{"seed_source": "checklist" \| "chat", "topics": [...]}` |

## Emission rules

### Server-side

- **`shown`** — After the stream list renders, enqueue a batch insert of one row per returned finding. Must not block the response; fire-and-forget on a background task. `source="stream"` (or `"email"` if we later emit from the digest mail).
- **`pin` / `dismiss` / `snooze` / `unpin` / `undismiss`** — Emitted in the same DB transaction as the `SignalView` write in `app/routes/findings.py`. If the `SignalView` upsert succeeds but the event insert fails, roll back both. A `SignalView` update without a log row is a bug.

### Client-side (`/api/signals/event`)

- **`view`** — IntersectionObserver; ratio ≥ 0.5 sustained for 500ms. Fire once per card per page load. Re-firing on the same card within 5 minutes is discarded server-side (see de-dup).
- **`dwell`** — Paired with `view`. When the card drops below the threshold or the user leaves the page (`visibilitychange`/`beforeunload`), compute `dwell_ms` and POST. If `dwell_ms < 500`, don't emit (noise floor).
- **`open`** — On click of the card's URL or expand action. Non-blocking.
- **Ratings** — Dedicated POST on click. Server writes the event row (no `SignalView` mirror; ratings are log-only).

### De-dup

Server rejects (silently, 204) a `view` event if the same `(user_id, finding_id)` already has a `view` within the last 5 minutes. Prevents scroll-thrash inflation. No de-dup on other types.

## API

New router: `app/routes/signal_events.py`.

### `POST /api/signals/event`

Authenticated (session cookie). Body:

```json
{
  "finding_id": 12345,      // optional; null for non-finding events
  "event_type": "view",     // required; must be in enum
  "value": 4200,            // optional
  "source": "stream",       // required
  "meta": { ... }           // optional
}
```

Response: `204 No Content` on accept (or de-dup reject). `400` on invalid `event_type` / `source`. `404` if `finding_id` given but doesn't exist.

### `POST /api/signals/events/batch`

Same shape, takes `{"events": [...]}`. Used for `shown` batch inserts and client-side flushes on `beforeunload`. Max 100 per request.

## Retention

- **Raw events:** keep 180 days. Nightly job (`app/jobs.py` — new `prune_signal_events`) deletes rows older than that.
- **Rollups (spec 02):** forever — summarized user profile lives separately.
- **Rationale:** if decay half-life is ~30d (spec 02 will decide), events older than 180d contribute <1% to any score. Safe to drop.

## Performance expectations

- Insert throughput: 100 events/sec sustained (well under SQLite single-writer ceiling).
- Query: "all events for user X, last 30d" ≤ 100ms at 100k rows. Covered by `(user_id, ts)` index.
- Rollup job (spec 02) reads full 30d window per user; expected to finish in ≤ 5s per user at 10k events/user.

## Privacy / data hygiene

- **No raw text** from findings in `meta`. Store IDs only — joins recover content at read time.
- **No URLs.** If we ever need source-origin tracking beyond `source` enum, add a column or controlled enum — not freeform URLs.
- **No IP / user-agent** in this table. Auth sessions already capture that; don't duplicate.

## Cold start

New users have no events. Spec 02 defines the cold-start default (recency + global popularity), but this spec supports it via:

- `onboarding_seed` events: first-login checklist or chat lets a user declare initial interests, which rollup treats as synthetic `rate_up` signals with lower weight.
- `count(events) < N` (threshold set in spec 02) is the "cold user" signal that toggles the ranker to fallback mode.

## Backfill

No backfill of historical `SignalView` rows into the log. The log starts empty on deploy. Rationale: pin/dismiss counts before launch are small, and conflating "I pinned this a month ago" with fresh signal would pollute the decay window. Ranker cold-starts for everyone.

## Open questions (resolve before spec 02)

1. **Should `shown` be logged at all?** It's high-volume (every scroll returns 20+ rows) and only useful as a CTR denominator. If we skip it, the rollup can't compute `view/shown` ratios — but it can still rank on positive signals alone. Proposal: log `shown` for 30 days, revisit.
2. **Dwell granularity.** Do we need millisecond precision, or bucket (0–2s, 2–10s, 10s+)? Proposal: store raw `dwell_ms`, bucket in the rollup. Cheaper to coarsen later than to recover precision.
3. **Email-origin events.** If the digest email becomes interactive (tracking pixel, click-through tokens), those emit `source="email"`. Out of scope for this spec, but taxonomy supports it.

## Acceptance criteria

1. Alembic migration creates `user_signal_events` with columns, indexes, and FKs above.
2. `app/routes/signal_events.py` implements `POST /api/signals/event` and `POST /api/signals/events/batch` with auth, validation, and de-dup.
3. `app/routes/findings.py` pin/dismiss/snooze/unpin/undismiss paths append an event row in the same transaction as the `SignalView` write. Transactional rollback verified by a test that forces the event insert to fail.
4. Stream list renderer enqueues `shown` batch insert after response. Does not block response latency (measured: p95 `/stream` latency unchanged ±10ms).
5. Stream card template includes client JS that emits `view` (IntersectionObserver) and `dwell` (on-exit) via the batch endpoint. `open` fires on URL click and expand.
6. `rate_up` / `rate_down` buttons present on the stream card and POST events on click. `more_like_this` / `less_like_this` remain in the event taxonomy for a future reason-tagged flow; no UI surface for them in v1 (keeps the card uncluttered — the weight delta vs thumbs is small and reason_tags would need their own UX).
7. `prune_signal_events` job registered in `app/jobs.py`, runs nightly, deletes rows older than 180d.
8. Index-coverage test: `EXPLAIN QUERY PLAN` on the rollup query uses the `(user_id, ts)` index.
9. Manual smoke: load `/stream`, scroll, click through — verify rows appear in `user_signal_events` with expected types, values, and `meta`.

## What this unblocks

With events flowing, spec 02 (preference rollup) can be written against real data rather than hypothetical traffic. We should let events accumulate for at least a week before finalizing rollup weights.
