# Spec 03 — Ranker

**Status:** Draft
**Owner:** Simon
**Depends on:** [01 — Signal Log](01-signal-log.md), [02 — Preference Rollup](02-preference-rollup.md)
**Unblocks:** 05 (integration)

## Purpose

Produce a personalized ordering of findings for a given user, with human-readable reasons. This is the single component the stream will call when rendering.

## Non-goals

- Producing the user profile (spec 02).
- Mutating user state. The ranker is pure: `(user_profile, findings[]) → scored[]`. No writes.
- Embedding similarity. Rule-based only in v1, but the interface supports a drop-in embedding scorer later.

## Design principles

1. **One interface, swappable implementations.** `Scorer.score(profile, finding) → (score, reasons[])`. Today's impl is rule-based; tomorrow's can be embedding-based or hybrid. Consumers never branch on implementation.
2. **Explainable on every call.** Every score carries a list of human-readable reasons. No "just trust it" scoring.
3. **Components are additive and named.** Score = sum of named components. We can log each component, tune weights independently, and show the user why.
4. **No LLM in the hot path.** Rendering the stream must not block on an LLM call. LLM is confined to preference chat (spec 04) and optional async explanation polish.
5. **Fallback is a first-class mode.** Cold-start users and empty-profile edge cases route through a dedicated fallback scorer, not degenerate zero-weight math.

## Interface

```python
# app/ranker/scorer.py
@dataclass(frozen=True)
class Reason:
    component: str       # "competitor_match" | "signal_type_match" | ...
    delta: float         # signed contribution to the final score
    detail: str          # human-readable, ≤ 80 chars

@dataclass(frozen=True)
class Scored:
    finding_id: int
    score: float         # typically [-2, +2], not clamped; higher = more relevant
    reasons: list[Reason]  # ordered by |delta| desc
    fallback: bool       # True when fallback scorer was used

class Scorer(Protocol):
    def score(self, profile: UserProfile, finding: Finding) -> Scored: ...

def rank(profile: UserProfile, findings: list[Finding], scorer: Scorer | None = None) -> list[Scored]: ...
```

`rank` is the public entry point. It picks the scorer based on `profile.cold_start` (fallback for cold, rule-based for warm), runs it, and returns `Scored` sorted by `score` desc.

## Score components (warm scorer)

Every component is a named additive term. Each emits a `Reason` only when `|delta| >= 0.05` — keeps the explanation list short.

| component              | formula                                                                 | typical range | notes                                                           |
| ---------------------- | ----------------------------------------------------------------------- | ------------- | --------------------------------------------------------------- |
| `competitor_match`     | `weight(competitor, finding.competitor)`                                | [−1, +1]      | Direct lookup from user profile vector.                         |
| `signal_type_match`    | `weight(signal_type, finding.signal_type)`                              | [−1, +1]      | Strong signal for "user cares about hires, not pricing".        |
| `source_match`         | `0.3 * weight(source, finding.search_provider or finding.source)`       | [−0.3, +0.3]  | De-emphasized — source is a weak proxy for preference.          |
| `topic_match`          | `0.5 * weight(topic, finding.topic)` (if finding.topic and weight exists in profile) | [−0.5, +0.5] | Skipped when evidence_count < 3 for that topic key (noise guard). |
| `keyword_match`        | `0.3 * weight(keyword, finding.matched_keyword)`                        | [−0.3, +0.3]  |                                                                 |
| `materiality_boost`    | `0.5 * (finding.materiality or 0)`                                      | [0, +0.5]     | Always additive; materiality is a global quality signal.        |
| `threat_level_boost`   | lookup: HIGH→+0.3, MEDIUM→+0.1, LOW→0, NOISE→−0.5                        | [−0.5, +0.3]  | From analyzer digest label (spec 02 data).                      |
| `recency_boost`        | `0.3 * exp(-ln(2) * age_days / 7)`                                      | [0, +0.3]     | 7-day half-life. Hottest findings get a nudge.                   |
| `novelty_penalty`      | `−0.5` if `SignalView.state in ("seen", "dismissed", "snoozed")` for this user-finding pair else `0` | [−0.5, 0] | Already-seen findings sink unless they're strong enough to re-surface. |
| `dismissal_floor`      | `−10` if explicitly dismissed (not just seen) within last 14 days       | large neg     | Ensures dismissed findings never re-surface unless user undismisses. Not a soft penalty — a hard floor. |

Final score = sum of all components. No clamping. Expected range [−2, +2] for typical findings; dismissed findings land near −10 and filter out naturally.

### Why additive, not multiplicative

Additive = debuggable. Each component's contribution is visible in the `reasons` list. Multiplicative would make "0.9 × 0.9 × 0.9" look weaker than it is and hide which factor drove the score. Tradeoff: additive doesn't punish cumulative weakness as hard, but we get interpretability.

### Why `novelty_penalty` and `dismissal_floor` differ

- `novelty_penalty` (−0.5): soft. A strong finding the user has seen can still climb back if the user's interests are well-matched — e.g. pinned a similar finding yesterday.
- `dismissal_floor` (−10): hard. Explicit "not interested" within 14 days should suppress absolutely. If the user wants it back, they undismiss.

## Fallback scorer (cold start)

Activated when `profile.cold_start = true` OR `profile.event_count_30d < 50`. Replaces the warm components with:

| component              | formula                                                                 | notes                                                           |
| ---------------------- | ----------------------------------------------------------------------- | --------------------------------------------------------------- |
| `materiality_boost`    | `1.0 * (finding.materiality or 0)`                                      | Stronger weight than warm scorer — quality signal does more work when we have no preference data. |
| `threat_level_boost`   | HIGH→+0.5, MEDIUM→+0.2, LOW→0, NOISE→−0.5                                | Slightly amplified.                                             |
| `recency_boost`        | `0.5 * exp(-ln(2) * age_days / 7)`                                      | Amplified. Fresh > stale when we can't personalize.             |
| `global_popularity`    | `0.3 * tanh(global_pin_count_30d / 5)`                                  | Simple "other users pinned this often" signal. Computed from a nightly materialized count, not live. |
| `novelty_penalty`      | same as warm                                                            |                                                                 |
| `dismissal_floor`      | same as warm                                                            |                                                                 |

Scored items return `fallback = True` so the UI can show a subtle "showing popular recent findings — rate some to personalize" nudge.

The fallback scorer also seeds learning: every impression in fallback mode is still logged (spec 01) and still rolls up (spec 02), so the user warms up naturally.

## Input batch size

The ranker ranks a list of findings the caller has already fetched. The caller (spec 05) decides how many — expected range 50–500 per request. The ranker does not paginate internally; it scores everything passed in.

Performance budget: 500 findings, warm scorer, ≤ 30ms. All lookups are dict-based off the `UserProfile` (loaded once per request); no per-finding DB calls.

## Explainability

Every `Scored` carries `reasons[]` ordered by `|delta|` descending. Example:

```
finding_id=12345 score=1.42 reasons=[
  Reason(component="signal_type_match", delta=+0.71, detail="You've engaged with 8 new_hire signals in the last 2 weeks"),
  Reason(component="competitor_match", delta=+0.55, detail="Indeed — weighted from 17 events"),
  Reason(component="materiality_boost", delta=+0.40, detail="High-materiality finding (0.80)"),
  Reason(component="recency_boost", delta=+0.21, detail="Published 2 days ago"),
  Reason(component="novelty_penalty", delta=-0.50, detail="You've already seen this"),
  ...
]
```

`detail` is generated deterministically from profile metadata (evidence_count, last_event_at). No LLM involvement.

### UI surface (spec 05 will consume)

Two levels of detail exposed to the stream:

- **Always visible:** top reason only, single line. "Matches your interest in new_hire signals." Kept subtle.
- **On expand:** full `reasons[]` list with deltas, for user debugging ("why am I seeing this?").

## Score caching

**No caching in v1.** The ranker is fast enough to run on every stream render. Caching introduces invalidation complexity (new event → cache stale) and the profile already acts as a compact cache of signal history.

Revisit if p95 stream latency exceeds budget.

## Weight configuration

Component weights (the multipliers in the formulas above) live in `app/ranker/config.py` as constants, not in the database. Rationale:

- They're code-level tuning, not user-level state.
- Changing them requires testing — code review is the right gate.
- Easy to make them env-var overridable for shadow-tuning if that becomes useful later (not in v1 — user is skipping shadow mode).

## Embedding swap

A future `EmbeddingScorer` implementing the same `Scorer` protocol:

- Computes `cosine(user_embedding, finding_embedding)` and contributes it as a new `embedding_match` component.
- Either replaces `topic_match` entirely or runs alongside and sums. Decide when we have embedding data to measure against.
- Requires: `profile.embedding` populated by a future spec 02 amendment, and `finding.embedding` computed at extraction time.

Nothing in this spec blocks that. The `Scorer` protocol is the only contract consumers depend on.

## Open questions

1. **Should dismissed findings ever re-surface?** Current proposal: hard floor for 14 days, then `novelty_penalty` only. After 14 days a dismissed finding is treated as "seen" and can climb back if strongly matched. Alternative: permanent dismissal. Proposal wins on recall; permanent wins on predictability. Going with 14-day floor.
2. **Weight components by profile confidence?** A user with 50 events has less reliable `signal_type_match` than one with 500. Could multiply warm weights by `min(1, event_count_30d / 200)` to ramp. Adds complexity; deferred until we see ranker quality complaints from early-warm users.
3. **Ranker output for findings with no matching dimensions?** E.g. new competitor we haven't surfaced before. All component lookups miss → score near 0 (materiality + recency only). This is correct behavior: unknown = neutral, not penalized. Worth documenting in UI copy.

## Acceptance criteria

1. `app/ranker/scorer.py` implements `Scorer` protocol, `RuleBasedScorer` (warm), and `FallbackScorer` (cold).
2. `app/ranker/rank.py` implements `rank(profile, findings)` routing.
3. Unit tests cover:
   - Each component in isolation (synthetic profile + finding → expected delta).
   - Additivity (final score = sum of deltas).
   - Novelty vs dismissal floor distinction.
   - Cold-start routing (cold user → fallback scorer, `fallback=True` in output).
   - Tie-breaking is stable (sort by score desc, then `created_at` desc).
4. `reasons[]` for every scored item is non-empty and ordered by `|delta|` desc.
5. Performance test: 500 findings × warm scorer ≤ 30ms p95.
6. `app/ranker/config.py` holds all component weights as named constants.
7. Manual smoke: for a warm user with known pins and dismisses, inspect rank order on a realistic feed and verify it matches intuition. Capture the `reasons[]` output to sanity-check explanations.

## What this unblocks

Spec 04 (preference chat) has a concrete thing to edit: the `taste_doc` slot and—indirectly—the vector via synthetic events. Spec 05 (integration) can wire `rank()` into `/stream` once the scorer exists.
