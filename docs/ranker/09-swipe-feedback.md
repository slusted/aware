# Spec 09 — Swipe interactions & competitor pinning

**Status:** Draft
**Owner:** Simon
**Depends on:** [01 — Signal Log](01-signal-log.md), [02 — Preference Rollup](02-preference-rollup.md), [08 — Semantic Ranking](08-semantic-ranking.md), and existing `_stream_card.html` swipe handlers in `app/templates/stream.html`.
**Unblocks:** real engagement data for spec 08's centroid (today the user has 1,600 `shown` events and 0 signed events — the embedding term contributes 0 to every card, see `scripts/diagnose_semantic_ranking.py`).

## Purpose

The stream already has mobile swipe — left = dismiss, right = pin — wired to the same `/partials/stream_view/{id}` endpoint as the (long-gone) action buttons. This spec evolves it on three axes:

1. **Left swipe gets a clearer mental model.** "Dismissed" reads as moderation; "hidden" reads as user choice. The card disappears from stream, and a checkbox in the filter bar brings it back. Behavior is unchanged from today; framing isn't.
2. **Right swipe stops being instant.** Today swipe-right pins the card on contact. Replace that with a flip animation that reveals two actions on the back: **Pin / save** and **Ask follow-up**. The user picks deliberately, or swipes back to cancel.
3. **Pinned findings rank highest on the competitor profile.** `/competitors/<id>` currently sorts findings by `created_at desc`. Surface pinned-by-the-current-user findings at the top of that list — pinning becomes a way to curate the per-competitor view.

The third axis matters for the overall feedback loop: today pins influence ranking *only* via the preference vector (a soft ripple effect on similar future findings). Making them visibly persistent on the competitor page gives the user immediate, durable proof that pinning matters — which is what gets them to do it.

## Non-goals

- Building the answer to a follow-up question. v1 captures the question text against the finding; *how* it gets answered (LLM round-trip, queued deepen run, manual review) is a spec 10 problem.
- Adding new event types to the spec 01 taxonomy. `pin`, `dismiss`, `undismiss` already cover the state changes; "follow-up question" rides as a `meta` field on a regular event (see *Backend*).
- Changing the stream's `score`-based ordering. Pinned-first sorting is **profile-page-only** (per the user's ask). Stream stays score-desc — pins influence ranking via the centroid + preference vector, not via a hardcoded "pinned slots first" rule.
- Re-introducing on-card pin/dismiss buttons for desktop. This spec only touches mobile swipe and the flip dialogue; desktop pinning happens via the same buttons that already exist on the back of the flipped card (mobile only) — desktop users are out of scope for v1.
- Migrating data, adding tables, or changing the `Finding`/`SignalView` models. The work is UI + one new question-storage column / table (see *Backend §2*).

## Design principles

1. **Same backend, evolved frontend.** Left swipe still hits `/partials/stream_view/{id}` with `state=dismissed`. Right swipe stops auto-posting on commit; instead it opens the back-of-card UI, which has its own buttons that hit the same endpoint with `state=pinned` or a new question-capture endpoint.
2. **Hidden, not dismissed.** UI copy says "hidden" everywhere user-facing — filter checkbox, tooltip, undo affordance. The DB keeps `state='dismissed'` because the event taxonomy and rollup math already speak that language. One translation layer in the template, not a schema change.
3. **Flip is reversible.** A right-swipe that opens the flip dialogue but never picks an action must not silently log a pin or dismiss. Cancelling the flip is a no-op (no event written).
4. **Pinned-first on profile is a sort, not a section.** Don't split the findings list into "Pinned" + "Other" sub-grids. Just sort: `(view.state == 'pinned') desc, created_at desc`. The visual cue is the existing `state-pinned` class on the card, plus the spec-01 `state-new` dot on the rest. One list, two implicit zones, already-styled.
5. **Pin from stream surfaces on profile.** The same pin event drives both surfaces — there is no per-page pin state. A finding pinned on the stream is pinned on the profile, and vice versa.

## States, in plain English

| user action               | event written                | SignalView.state | UI effect (stream)                          | UI effect (competitor profile)               |
| ------------------------- | ---------------------------- | ---------------- | ------------------------------------------- | -------------------------------------------- |
| Swipe left                | `dismiss`                    | `dismissed`      | Hidden from default view; "Show hidden" filter brings it back. | Hidden until "Show hidden" filter active.    |
| Swipe right → Pin         | `pin`                        | `pinned`         | Card stays in list with `state-pinned` style. | Floats to the top, `state-pinned` style.    |
| Swipe right → Ask q       | `pin` + `meta.question = "…"` | `pinned`         | Same as Pin (pinning is implied — see *Open questions §1*). | Same as Pin. |
| Swipe right → Cancel flip | (none)                        | unchanged        | No-op.                                      | No-op.                                       |
| Show-hidden filter on     | (none)                        | n/a              | Hidden cards reappear with their `state-dismissed` style. | Same. |
| Tap "Unhide" on a hidden card | `undismiss`               | `pinned`? `seen`? See *Open questions §2*. | Card returns to default style. | Card returns to default style. |

The events listed here are the same names already in `EVENT_WEIGHTS` (config.py) — no taxonomy churn.

## UI — stream

### Left swipe → hide

Mechanically identical to today. Renames in the user-facing text:

- Confirmation toast (if/when one is added — not in v1): "Hidden. Undo →"
- Filter checkbox (in `stream.html` lines 71–75): change copy from "Show dismissed" to "Show hidden". Form field name stays `include_dismissed` so server code is unchanged.
- Card style when state is `dismissed`: keep the existing dimmed look. Existing `.state-dismissed` CSS class.

### Right swipe → flip

The card is a 3D flip target. CSS-only animation (`transform: rotateY(180deg)`, `backface-visibility: hidden`, single perspective wrapper around the card list). Flip is triggered by:

- A right swipe past `COMMIT_PX` (the existing 80 px threshold in stream.html line 157). The current logic posts immediately; instead it adds a `.is-flipped` class and removes `is-swiping`.
- A tap on a small "↻" affordance in the card's top-right corner (mobile + desktop, since flipping is the only desktop pin path in v1). 24×24 px, only visible on hover (desktop) or always (mobile). `aria-label="Pin or ask follow-up"`.

The back of the card shows three actions vertically:

```
┌──────────────────────────────┐
│  Pin / save                  │   ← primary; emits `pin`
├──────────────────────────────┤
│  Ask follow-up               │   ← textarea-revealing; emits `pin` + meta.question
├──────────────────────────────┤
│  Cancel                      │   ← flips back; no event
└──────────────────────────────┘
```

The "Ask follow-up" action expands inline to reveal a `<textarea maxlength=500>` plus a "Send" button. Submitting writes the question (see *Backend §2*) and pins. Cancelling collapses back to the three-button view.

CSS lives in the existing stream stylesheet under a new `.signal-card.is-flipped` block. JS lives next to the existing swipe handler in `stream.html` — no new file.

### Filter bar — "Show hidden"

Existing checkbox. Rename copy. No server change. (`include_dismissed` form field stays; only the label is updated.) Optional small-win: add a count next to it — `Show hidden (12)` — using a cheap `COUNT(*) WHERE state='dismissed'` on the same query path. Nice-to-have, not blocking.

### Visual feedback

- Mid-swipe, the existing `--swipe-left-opacity` / `--swipe-right-opacity` red/yellow tints continue to fade in (today's CSS).
- Past the commit threshold, swap the right-side tint for the back-of-card "Flip to choose" hint so the user knows the destination changed.
- Once flipped, the card's height matches its front; the back uses `position: absolute` to overlay so the list doesn't reflow.

## UI — competitor profile

### Pinned-first sort

`competitor_profile` route in `app/ui.py` line 187 currently:

```python
findings = (
    db.query(Finding)
    .filter(Finding.competitor == c.name)
    .order_by(Finding.created_at.desc())
    .limit(30)
    .all()
)
```

Becomes:

```python
findings = (
    db.query(Finding)
    .outerjoin(SignalView, and_(
        SignalView.finding_id == Finding.id,
        SignalView.user_id == user.id,
    ))
    .filter(
        Finding.competitor == c.name,
        # Hide hidden by default — same model as stream. A profile-page
        # "Show hidden" toggle is out of scope for v1; if it's needed,
        # it's the same checkbox shape as on /stream.
        or_(SignalView.state.is_(None), SignalView.state != "dismissed"),
    )
    .order_by(
        case((SignalView.state == "pinned", 0), else_=1),
        Finding.created_at.desc(),
    )
    .limit(30)
    .all()
)
```

Spec 01 made this page render `_stream_card.html` with `show_rating=False, show_competitor=False, expandable=True`. That stays. The `state` flag is already in scope (the partial reads `view.state`), so pinned cards already pick up the `state-pinned` styling — no template change.

### Follow-up question display

If the user asked a follow-up on a finding, that question is shown in the expanded panel (the spec-01 expand-on-click block). One additional `<div class="follow-up-question">` row inside the existing provenance list:

```
Asked: "How does this compare to LinkedIn's launch last quarter?"
```

No answer is shown in v1 (we haven't built the answer side). The user sees their question echoed back as proof of capture. Spec 10 wires the answer.

## Backend

### §1 — Existing endpoint stays

`/partials/stream_view/{finding_id}` already accepts `state` ∈ `{seen, pinned, dismissed, snoozed}` and dual-writes to both `SignalView` (current state) and `UserSignalEvent` (the append-only log the rollup reads). No change.

The swipe-left commit posts `state=dismissed` exactly as today. The new "Pin / save" button on the flipped card posts `state=pinned` exactly as today. The route doesn't know about the flip; it sees the same form bodies.

### §2 — New: question capture

One new endpoint, one new column.

- **Column:** `signal_views.question` (`Text`, nullable). One column on the existing per-user view row, not a new table — keeps the join simple, matches the "this is the user's annotation of the finding" model.
- **Endpoint:** `POST /partials/finding/{finding_id}/question`
  - Body (form-encoded): `question` (string, ≤500 chars, required, stripped, non-empty).
  - Behavior: upsert SignalView with `state='pinned'` (asking a question implies pinning — see *Open questions §1*) and `question=<text>`. Append a `UserSignalEvent` with `event_type='pin'`, `value=None`, `meta={"question_chars": len(question), "via": "swipe_flip"}`. **Do not store the question text in the event log** — it lives on `SignalView` so it stays editable and isn't duplicated on every rebuild.
  - Response: re-rendered card via the existing `_stream_card.html` template (matches `partial_stream_view`'s response shape), so the swap target is the same as the existing pin/dismiss path.
- **Validation:** length cap server-side (matches the textarea `maxlength`); reject empty after `.strip()` with a 400.

Why a column on `SignalView` and not a separate `finding_questions` table:

- The question is per (user, finding) — exactly what `SignalView` already keys on.
- One row per user-finding pair holds all the annotations together; reading "the user's full take on this finding" is one row, not a join.
- The data is small (≤500 chars × at most thousands of pins per user) and unstructured. A table buys nothing.
- If question history (multiple questions over time) ever matters, *that's* what an audit log is for — and `UserSignalEvent` already records the timestamp of every pin.

### §3 — Migration

One Alembic migration:

```python
op.add_column(
    "signal_views",
    sa.Column("question", sa.Text(), nullable=True),
)
```

That's the entire schema change for spec 09.

## Templates

- `app/templates/_stream_card.html` — gain a `.signal-card-back` block (currently the card has only a front). The back is rendered unconditionally but hidden until `.is-flipped` is set. Keeps the partial's response shape unchanged so the existing HTMX swap target works for both pin paths.
- `app/templates/stream.html` — JS additions next to the existing swipe handler (no new file).
- `app/templates/competitor_profile.html` — no changes; the partial already reads `view.state`. The new sort happens in the route, not the template.

## Acceptance criteria

1. **Hide via swipe-left** — swiping a card left past 80 px posts `state=dismissed`, the card disappears on next list refresh, and the "Show hidden" filter (renamed from "Show dismissed") brings it back.
2. **Flip via swipe-right** — swiping a card right past 80 px flips it (CSS animation, no DB write). Three buttons on the back: Pin / save, Ask follow-up, Cancel.
3. **Pin via flip** — tapping "Pin / save" posts `state=pinned`, the card flips back to the front in the `state-pinned` style, and a `pin` event lands in `user_signal_events`.
4. **Question capture** — tapping "Ask follow-up" reveals an inline textarea (≤500 chars). Submitting writes `signal_views.question`, sets `state='pinned'`, and writes a `pin` event with `meta.via='swipe_flip'`. Empty / whitespace-only input is rejected with a 400.
5. **Cancel is a no-op** — flipping the card and tapping Cancel writes nothing to `signal_views` or `user_signal_events`.
6. **Pinned-first on competitor profile** — `/competitors/<id>` lists pinned-by-current-user findings before unpinned ones. Within each group, sorted by `created_at desc`. Hidden findings excluded.
7. **Question shown on profile** — when a finding has `signal_views.question` set for the current user, the expanded panel on the competitor profile renders an "Asked: …" row.
8. **Desktop ↻ affordance** — a small flip button in the card's top-right corner triggers the same flip as the swipe (so desktop has a path to pinning even without touch).
9. **No regression** — `/stream` with no swipe and no flip behaves byte-identically to main. Existing rating buttons (👍/👎) still work.
10. **One migration** — `alembic upgrade head` adds `signal_views.question`. No other schema change.

## Verification

A new `scripts/verify_swipe_feedback.py` covering:

- Server-side question endpoint accepts a 200-char string, rejects empty, rejects >500 chars.
- Posting a question writes `signal_views.question`, sets `state='pinned'`, and appends a `UserSignalEvent` row with `event_type='pin'` and `meta.via='swipe_flip'`.
- Cancelling a flip (i.e., not posting at all) leaves both tables untouched.
- Competitor profile route returns pinned findings first, then unpinned by date.
- Hidden findings (`state='dismissed'`) excluded from the profile list by default.
- Schema check: `signal_views` has a `question` column, nullable Text.

## Open questions

1. **Does asking a follow-up imply pinning?** Current proposal: yes. A user willing to type 50 characters about a finding has implicitly told us they care about it; treating that as a pin saves a step and makes the rollup learn from the engagement. Alternative: ask-without-pin (state stays `seen`). Going implicit-pin for v1 because the alternative requires another button on the back of the card and the cognitive cost of "do I pin too?" outweighs the value.
2. **What state does an "unhide" produce?** Tapping a hidden card to bring it back could set `state` to `pinned` (overshoot — they only wanted to unhide), `seen` (correct neutral), or `null` (cleanest, but then the row stays in the table forever). Going with `seen` — it's neutral, keeps the row, and pairs naturally with an `undismiss` event in the log (already in the taxonomy).
3. **Should the back-of-card "Pin / save" be re-tappable to unpin?** Tapping a pinned card again to unpin is a sensible expectation. For v1, the answer is "swipe left" — if you decide a pin was wrong, hide it. Tap-to-unpin is a v1.1 concern; the back-of-card UI gets a third button ("Unpin") only when the current state is already `pinned`.
4. **Do questions need a per-user privacy boundary on the profile?** Today `_stream_card.html`'s expand panel is read-only metadata. If spec 04 (preference chat) ever surfaces another user's question on a shared profile view, we need to scope the "Asked: …" row to the current user. v1 only ever queries `SignalView.user_id == current_user.id`, so the answer is "yes, naturally" — but worth re-checking once spec 10 lands and answers might be shared.
5. **Mobile-only flip vs all-screens flip?** The current swipe is mobile-only (`@media max-width: 820px`). The new desktop ↻ button is the only path to pinning on desktop in v1. If desktop users find the flip animation disorienting on a fixed-width card, fall back to a small inline popover instead. Eyeball at implementation time.

## What this unblocks

- **Spec 08's centroid actually doing work.** Today the user has 1,600 `shown` events and zero signed engagement events; the embedding term contributes 0 to every card. A workable swipe-right flow is the bottleneck.
- **Spec 10 — answering follow-up questions.** Now there's a concrete data point (`signal_views.question`) for whatever answer pipeline gets built next: synchronous LLM, queued deepen run, or human review.
- **Per-finding curation on the competitor page.** The "this is what matters" view of a competitor becomes the user's pinned set, sorted by recency — which is the read most strategy reviews want.
- **A simpler desktop pin affordance later.** The back-of-card UI is the canonical pin surface; if/when the rating thumbs (👍/👎) get reconsidered, the flip is where pin/dismiss/snooze converge.
