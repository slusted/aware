"""Closed taxonomy for UserSignalEvent.event_type / .source, plus the
validator used at insert time. Adding a new event_type requires a spec
amendment (docs/ranker/01-signal-log.md) — do not add values here
without updating the spec first.
"""
from __future__ import annotations


# ---- Event types -----------------------------------------------------------

# Server-emitted implicit events. Fired from /stream render or from the
# pin/dismiss/snooze routes in app/routes/findings.py. Client code MUST NOT
# POST these — the API rejects them.
SERVER_EMITTED_TYPES: frozenset[str] = frozenset({
    "shown",      # card rendered in a stream response
    "pin",
    "dismiss",
    "snooze",
    "unpin",
    "undismiss",
    "onboarding_seed",   # synthetic; fired during first-login seeding
})

# Client-emitted implicit events. POSTed via /api/signals/event(s).
CLIENT_EMITTED_TYPES: frozenset[str] = frozenset({
    "view",       # ≥50% visible for ≥500ms
    "dwell",      # finalized on exit, payload is dwell_ms in `value`
    "open",       # click-through or expand
})

# Explicit user ratings. Client-emitted.
RATING_TYPES: frozenset[str] = frozenset({
    "rate_up",
    "rate_down",
    "more_like_this",
    "less_like_this",
})

# System events. Emitted by internal flows (preference chat in spec 04).
SYSTEM_TYPES: frozenset[str] = frozenset({
    "chat_pref_update",
})

ALL_EVENT_TYPES: frozenset[str] = (
    SERVER_EMITTED_TYPES | CLIENT_EMITTED_TYPES | RATING_TYPES | SYSTEM_TYPES
)

# Types the client is allowed to POST. Everything else is server-only.
CLIENT_POSTABLE_TYPES: frozenset[str] = CLIENT_EMITTED_TYPES | RATING_TYPES

# Types that require a finding_id. chat_pref_update and onboarding_seed can
# have a null finding_id (they describe preference state, not a reaction to
# a specific finding).
REQUIRES_FINDING_TYPES: frozenset[str] = (
    CLIENT_EMITTED_TYPES | RATING_TYPES | frozenset({"pin", "dismiss", "snooze", "unpin", "undismiss", "shown"})
)

# Explicit high-intent types. Spec 02 uses this set to trigger an
# immediate per-user rollup rebuild instead of waiting for the nightly job.
EXPLICIT_SIGNAL_TYPES: frozenset[str] = RATING_TYPES | frozenset({"chat_pref_update"})


# ---- Event sources ---------------------------------------------------------

VALID_SOURCES: frozenset[str] = frozenset({
    "stream",       # the main /stream surface
    "detail",       # a single-finding detail view
    "email",        # digest email click-through (future)
    "chat",         # preference chat
    "onboarding",   # first-login seed flow
    "system",       # background jobs / synthetic events
})


# ---- Validation ------------------------------------------------------------

class EventValidationError(ValueError):
    """Raised for any violation of the closed taxonomy. Callers turn this
    into a 400 at the API boundary."""


def validate_event(
    *,
    event_type: str,
    source: str,
    finding_id: int | None,
    client_origin: bool,
) -> None:
    """Validate a prospective event insert. Raises EventValidationError on
    any violation. `client_origin=True` when the event is arriving from the
    /api/signals/event(s) endpoint — enforces the client-postable subset.
    """
    if event_type not in ALL_EVENT_TYPES:
        raise EventValidationError(f"unknown event_type: {event_type!r}")
    if source not in VALID_SOURCES:
        raise EventValidationError(f"unknown source: {source!r}")
    if client_origin and event_type not in CLIENT_POSTABLE_TYPES:
        raise EventValidationError(
            f"event_type {event_type!r} is server-only and cannot be POSTed"
        )
    if event_type in REQUIRES_FINDING_TYPES and finding_id is None:
        raise EventValidationError(
            f"event_type {event_type!r} requires a finding_id"
        )


# ---- Dwell bucketing -------------------------------------------------------
#
# Spec 01 leaves dwell_ms as raw data in the event row; spec 02's rollup
# buckets it when computing weight contributions. Helper lives here so the
# bucket boundaries are defined alongside the taxonomy.

DWELL_NOISE_FLOOR_MS: int = 500
DWELL_SHORT_MAX_MS: int = 2_000
DWELL_MEDIUM_MAX_MS: int = 10_000


def dwell_bucket(dwell_ms: float | None) -> str | None:
    """Return 'noise' | 'short' | 'medium' | 'long' | None.

    None means the value was missing or the event wasn't a dwell event.
    'noise' means below the 500ms floor — spec 01 says clients shouldn't
    emit these, but the validator accepts them so we return the bucket
    for completeness and let the rollup ignore them.
    """
    if dwell_ms is None:
        return None
    if dwell_ms < DWELL_NOISE_FLOOR_MS:
        return "noise"
    if dwell_ms < DWELL_SHORT_MAX_MS:
        return "short"
    if dwell_ms < DWELL_MEDIUM_MAX_MS:
        return "medium"
    return "long"
