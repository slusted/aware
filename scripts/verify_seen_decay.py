"""End-to-end verification for the stand-in seen-decay term
(docs/ranker/07-seen-decay.md).

Pure in-memory test over app/ranker/present.py — no DB, no FastAPI.
Mirrors scripts/verify_cluster_diversity.py so the harness stays
uniform.

Usage:
    python scripts/verify_seen_decay.py
Exit code: 0 on all pass, 1 on any fail.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import models  # noqa: E402
from app.ranker import config as rcfg  # noqa: E402
from app.ranker.present import default_score, present  # noqa: E402


# ── Harness ─────────────────────────────────────────────────────────

_passes = 0
_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global _passes
    status = "PASS" if ok else "FAIL"
    line = f"  [{status}] {name}"
    if detail:
        line += f"  — {detail}"
    print(line)
    if ok:
        _passes += 1
    else:
        _failures.append(name)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


NOW = datetime(2026, 4, 24, 12, 0, 0)


def mk(
    *,
    id: int,
    title: str = "Indeed launches pricing",
    competitor: str = "Indeed",
    signal_type: str | None = "new_hire",
    materiality: float | None = 0.7,
    age_days: float = 0.0,
) -> models.Finding:
    """Detached Finding — never touches a DB."""
    f = models.Finding()
    f.id = id
    f.competitor = competitor
    f.signal_type = signal_type
    f.topic = None
    f.source = "news"
    f.title = title
    f.materiality = materiality
    f.created_at = NOW - timedelta(days=age_days)
    f.hash = f"h{id}"
    return f


# ── §1: default_score math ──────────────────────────────────────────
section("default_score math")

base = mk(id=1)
baseline = default_score(base, now=NOW)
check("seen_count defaults to 0 → baseline unchanged vs no arg",
      default_score(base, now=NOW, seen_count=0) == baseline)

per_view = rcfg.STANDIN_SEEN_DECAY_PER_VIEW
max_views = rcfg.STANDIN_SEEN_DECAY_MAX_VIEWS

# One countable view → exactly one PER_VIEW penalty.
expected_1 = baseline - per_view
got_1 = default_score(base, now=NOW, seen_count=1)
check("seen_count=1 → penalty = PER_VIEW exactly",
      abs(got_1 - expected_1) < 1e-9,
      detail=f"expected={expected_1:.4f} got={got_1:.4f}")

# Cap: anything above MAX_VIEWS clamps to MAX × PER_VIEW.
expected_cap = baseline - (per_view * max_views)
got_cap = default_score(base, now=NOW, seen_count=max_views + 5)
check("seen_count >> MAX clamps at MAX_VIEWS × PER_VIEW",
      abs(got_cap - expected_cap) < 1e-9,
      detail=f"expected={expected_cap:.4f} got={got_cap:.4f}")

# Negative counts treated as 0 (defensive — shouldn't happen from DB).
check("seen_count=-3 → no bonus, no change",
      default_score(base, now=NOW, seen_count=-3) == baseline)


# ── §2: present() wiring ────────────────────────────────────────────
section("present() wiring")

a = mk(id=10, title="Indeed raises Series D", materiality=0.6, age_days=1)
b = mk(id=11, title="LinkedIn ships AI recruiter", materiality=0.6, age_days=1)

# No map → both identical after clustering.
cards_neutral = present([a, b], now=NOW)
check("seen_count_by_id=None → both findings score identically",
      len(cards_neutral) == 2 and abs(cards_neutral[0].score - cards_neutral[1].score) < 1e-9)

# Sink only `a` with 1 prior view. `b` should now rank strictly higher.
cards_sink_a = present([a, b], now=NOW, seen_count_by_id={a.id: 1})
top = cards_sink_a[0].lead.id
check("seen findings sink below unseen peers",
      top == b.id,
      detail=f"top lead id = {top} (expected {b.id})")

# Score delta matches exactly one PER_VIEW.
delta = cards_sink_a[0].score - cards_sink_a[1].score
check("score gap between unseen vs 1-view-seen equals PER_VIEW",
      abs(delta - per_view) < 1e-9,
      detail=f"gap={delta:.4f} per_view={per_view:.4f}")

# Selective application: only findings named in the map are penalized.
c = mk(id=20, title="Workday adds feature", materiality=0.9, age_days=0)
d = mk(id=21, title="Workday hires exec",   materiality=0.9, age_days=0, signal_type="funding")
e = mk(id=22, title="Workday pricing tier", materiality=0.9, age_days=0, signal_type="price_change")
cards_sel = present([c, d, e], now=NOW, seen_count_by_id={d.id: 1, e.id: max_views + 10})
by_id = {card.lead.id: card.score for card in cards_sel}
check("selective: c unaffected",
      abs(by_id[c.id] - default_score(c, now=NOW)) < 1e-9)
check("selective: d loses exactly PER_VIEW",
      abs(by_id[d.id] - (default_score(c, now=NOW) - per_view)) < 1e-9)
check("selective: e loses exactly MAX_VIEWS × PER_VIEW (clamped)",
      abs(by_id[e.id] - (default_score(c, now=NOW) - per_view * max_views)) < 1e-9)


# ── §3: custom score_fn bypasses the map ────────────────────────────
section("custom score_fn precedence")

f1 = mk(id=30, title="alpha beta gamma", materiality=0.5)
f2 = mk(id=31, title="delta epsilon zeta", materiality=0.5)

def const_score(_f: models.Finding) -> float:
    return 42.0

cards_custom = present(
    [f1, f2],
    now=NOW,
    score_fn=const_score,
    seen_count_by_id={f1.id: 100},
)
check("custom score_fn ignores seen_count_by_id (both score 42.0)",
      all(abs(card.score - 42.0) < 1e-9 for card in cards_custom))


# ── Summary ─────────────────────────────────────────────────────────
print()
print(f"{_passes} passed, {len(_failures)} failed")
if _failures:
    for name in _failures:
        print(f"  FAIL: {name}")
    sys.exit(1)
sys.exit(0)
