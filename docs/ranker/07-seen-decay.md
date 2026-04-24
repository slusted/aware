# Spec 07 — Stand-in Seen Decay

**Status:** Draft
**Owner:** Simon
**Depends on:** [01 — Signal Log](01-signal-log.md) (data already captured), [06 — Cluster & Diversity](06-cluster-diversity.md) (scorer lives here)
**Obsoleted by:** [03 — Ranker](03-ranker.md) when its `novelty_penalty` component ships. Until then, this is how "seen = sink" gets into the stream.

## Purpose

Findings the user has already looked at in previous sessions should drop down the list, not hang there forever. Today's stand-in scorer (`default_score` in `app/ranker/present.py`) only sees `materiality + recency` — it has no awareness of impressions, so a high-materiality card from last week keeps its spot no matter how many times it's been viewed.

Spec 03's real ranker will handle this via a `novelty_penalty` component. But 03 is a much bigger build, and the user has flagged this as the *main* complaint with base-level quality before personalization. Small targeted fix ships now; spec 03 supersedes it later.

## Non-goals

- Personalization (spec 03's job — this spec applies the same decay shape to every user).
- Re-ranking pinned content (pin is already a strong positive signal elsewhere; seen-decay is additive and small).
- Inferring "sessions" explicitly. A single trailing exclusion window is close enough.
- Schema changes. The data's already in `user_signal_events`.

## Design principles

1. **Zero schema churn.** All inputs come from `user_signal_events`, populated since spec 01 shipped.
2. **Additive, visible, cap it.** Same shape as the rest of `default_score`: a named term, bounded contribution, no interaction terms.
3. **Current-session views don't affect current-session ordering.** A fixed trailing-60-minute window carves out "what the user is looking at right now" so scrolling and reloading in-session doesn't reshuffle the list.
4. **Each countable view = one unit of prior exposure.** The client fires `view` once per card per page-load (see `app/static/signals.js`), so N countable views ≈ N prior times the user loaded the stream with this card on screen. The math is a direct count, not a bucketed "days seen" proxy.
5. **Interim, not permanent.** Lives inside `default_score`. When spec 03's `RuleBasedScorer` lands, it owns this logic (with per-user weights) and this code gets deleted.

## Signal

Count `view` or `open` events for this (user, finding) where `ts < now − EXCLUDE_MINUTES`.

- `view` = client-emitted when a card is ≥50% visible for ≥500ms. Fires **once per card per page load** (re-entering the viewport does not re-fire). So one view event ≈ one stream-load exposure.
- `open` = click-through or expand. Strong engagement, and someone who opened it has certainly absorbed it.
- `shown` is **not** counted. `shown` fires server-side for every card in every render — using it would sink everything the moment the stream loads twice, regardless of whether the user actually saw the card.
- Events inside the trailing 60-minute window are excluded. That captures "the current session": scrolling at 10:00 and reloading at 10:30 sees no penalty; reloading at 11:30 does, because the 10:00 view is now past the carve-out.

### Why 60 minutes, not calendar-day

Earlier drafts excluded "today" (UTC). That has two problems:

1. Coarse at both ends. A short lunch break after a morning scroll still counts as "today" and so the penalty never kicks in. A session that crosses midnight gets penalized mid-session.
2. Too lenient. The user's own complaint — "next session, it should be much further down" — treats "next session" as "I came back later," not "I came back tomorrow." A 60-minute gap is a reasonable proxy for "new visit."

60 minutes is tunable via `STANDIN_SEEN_DECAY_EXCLUDE_MINUTES`.

## Decay shape

```
seen_count = count(view|open events where ts < now − EXCLUDE_MINUTES)
penalty    = −STANDIN_SEEN_DECAY_PER_VIEW · min(seen_count, STANDIN_SEEN_DECAY_MAX_VIEWS)
```

With starting constants:

| constant                              | value | effect                                                       |
| ------------------------------------- | ----- | ------------------------------------------------------------ |
| `STANDIN_SEEN_DECAY_PER_VIEW`         | 0.25  | Each countable view = −0.25                                  |
| `STANDIN_SEEN_DECAY_MAX_VIEWS`        | 3     | Cap at −0.75 total                                           |
| `STANDIN_SEEN_DECAY_EXCLUDE_MINUTES`  | 60    | Events inside the trailing hour are ignored                  |

Reference points (assuming materiality 0.7 + recency ≈ 0.3 ⇒ baseline ≈ 1.0):

- 0 prior views: no penalty. Top of the stream.
- 1 prior view (saw it 2 hours ago): −0.25 → score ~0.75. Sinks a few slots.
- 3+ prior views (loaded the stream 3+ times with it on screen): −0.75 → score ~0.25. Buried, but a genuinely material item still clears zero and holds a tail position.

The cap is deliberate: we want persistently high-value items to stay visible, just demoted. Permanent suppression is what `dismiss` is for.

## Interface change

`app/ranker/present.py`:

```python
def default_score(
    finding: Finding,
    *,
    now: datetime | None = None,
    seen_count: int = 0,          # NEW
) -> float: ...

def present(
    findings: list[Finding],
    *,
    score_fn: Callable[[Finding], float] | None = None,
    now: datetime | None = None,
    seen_count_by_id: dict[int, int] | None = None,   # NEW
    mmr_window: int | None = None,
    mmr_lambda: float | None = None,
    jaccard_threshold: float | None = None,
) -> list[ClusterCard]: ...
```

`present()` threads `seen_count_by_id.get(f.id, 0)` into the default scorer. When the caller supplies its own `score_fn`, `seen_count_by_id` is ignored (the custom scorer owns its own math).

Defaulting to `None`/`0` keeps every existing caller and test working unchanged.

## Caller change

`app/ui.py::_stream_query` builds the map once per request, after fetching `findings` and before calling `_present_clusters`:

```sql
SELECT finding_id, COUNT(*) AS n
FROM   user_signal_events
WHERE  user_id = :user_id
  AND  event_type IN ('view', 'open')
  AND  finding_id IN (:ids)
  AND  ts < :cutoff        -- cutoff = now - EXCLUDE_MINUTES
GROUP  BY finding_id;
```

One query, O(1) per request regardless of finding count. Results stuff into a `dict[int, int]` and pass to `present()`.

## Test plan

Unit tests (`scripts/verify_seen_decay.py`):

1. `seen_count=0` → `default_score` equals pre-change formula (`materiality + recency`).
2. `seen_count=1` → penalty = −0.25 applied, score drops by exactly that.
3. `seen_count=5` → penalty clamps at max-views × per-view = −0.75, not −1.25.
4. `seen_count_by_id=None` in `present()` → every finding gets 0 penalty (equivalence with today's behaviour).
5. `seen_count_by_id={2: 1, 3: 5}` → only findings 2 and 3 are penalized; finding 2 gets −0.25, finding 3 clamps at −0.75.
6. Custom `score_fn` supplied → `seen_count_by_id` is ignored (documented precedence).
7. Ordering: given two identical findings (same materiality, same recency) and one has `seen_count=2`, the unseen one ranks strictly higher after `present()`.

Manual smoke:

8. Load `/stream`, scroll so a few cards fire `view` events, reload within a minute. Order unchanged (views within the 60-min window don't count).
9. Wait 70+ minutes (or temporarily set `EXCLUDE_MINUTES=0` in dev), reload. Those cards sink.

## Rollout / reversibility

- No migration. All data already present.
- Feature is one subtraction behind a config constant. Setting `STANDIN_SEEN_DECAY_PER_VIEW = 0.0` turns it off without a revert.
- When spec 03 ships, delete `seen_count` from `default_score`, delete `seen_count_by_id` from `present()`, delete the caller query. The `novelty_penalty` component in the real scorer replaces it.

## Open questions

1. **Include `shown` as a weak signal?** Current proposal says no — too noisy. If "I scrolled past it without engaging" turns out to be a signal people want respected, add it at a lower weight (e.g. 0.1 per event) alongside view/open's 0.25. Deferred until any user complains about seen-but-not-viewed items staying high.
2. **Should pinned findings be immune?** No in v1 — the additive math already keeps strong finds afloat (`−0.75` floor vs `+1.2` typical good score), and threading pin state through would add a second query. Revisit if pinned items visibly get buried.
3. **Decay the penalty over time?** A view from a year ago probably shouldn't penalize as hard as one from yesterday. Deferred — retention on `user_signal_events` is 180 days, and the cap at 3 already limits how punitive things get. Spec 03's ranker can layer exponential decay on each event's contribution when it lands.
4. **Per-user exclusion window once spec 03 lands?** A user who always re-reads the same competitor probably wants more forgiveness; a user who hates seeing anything twice wants less. Out of scope here — spec 03's per-user weights subsume it.
