"""Daily-volume stacked-bar chart for the dashboard.

Returns a `ChartView` shaped for direct Jinja rendering — the template
only iterates, never computes. One query per render. No LLM, no I/O
beyond the DB session.

Modes:
  - "type"       → stack segments are `signal_type` (closed taxonomy).
  - "competitor" → stack segments are the top N competitors in the window;
                   everything else collapses to `Other`.

Bar height = total findings that day. Segment heights sum to the bar.
"""
from __future__ import annotations

import zlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal

from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import Finding


Mode = Literal["type", "competitor"]

ALLOWED_DAYS = (7, 30, 90)
TOP_N_COMPETITORS = 10


# Closed signal-type palette. Values align with the chip colors in
# app/static/style.css (.chip-body.type-*) so the chart and the stream
# filters speak the same visual language. The four taxonomy values
# without a chip color get reasonable neutrals.
SIGNAL_TYPE_COLORS: dict[str, str] = {
    "funding":         "#4cb782",
    "new_hire":        "#60a5fa",
    "product_launch":  "#c084fc",
    "integration":     "#2dd4bf",
    "price_change":    "#e0a500",
    "messaging_shift": "#f472b6",
    "momentum_point":  "#fb7185",
    "news":            "#9ca3af",
    "voc_mention":     "#fca5a5",
    "other":           "#94a3b8",
    "unknown":         "#cbd5e1",
}

# Legend / stack order for `type` mode. Matches SIGNAL_TYPES in app/ui.py
# so the chart reads in the same order as the stream filters. `unknown`
# is appended for rows with NULL signal_type.
SIGNAL_TYPE_ORDER: tuple[str, ...] = (
    "funding", "new_hire", "product_launch", "integration", "price_change",
    "messaging_shift", "voc_mention", "news", "momentum_point", "other",
    "unknown",
)

# Rotating palette for competitor mode. Deliberately distinct from the
# signal-type palette so toggling modes is visually unambiguous.
COMPETITOR_PALETTE: tuple[str, ...] = (
    "#60a5fa", "#f472b6", "#4cb782", "#e0a500", "#c084fc",
    "#2dd4bf", "#fb7185", "#f97316", "#a78bfa", "#38bdf8",
    "#facc15", "#34d399",
)

OTHER_COLOR = "#94a3b8"
OTHER_LABEL = "Other"


@dataclass
class Segment:
    key: str
    label: str
    color: str
    total: int
    detail: str | None = None  # tooltip content for "Other"


@dataclass
class DayBar:
    bucket: date
    total: int
    parts: list[tuple[str, int]] = field(default_factory=list)


@dataclass
class ChartView:
    mode: Mode
    days: int
    bars: list[DayBar]
    segments: list[Segment]
    y_max: int
    total_findings: int


def stable_color_for_competitor(name: str) -> str:
    """Deterministic across processes — must not depend on Python's salted hash()."""
    idx = zlib.crc32(name.encode("utf-8")) % len(COMPETITOR_PALETTE)
    return COMPETITOR_PALETTE[idx]


def _parse_bucket(raw) -> date:
    """`func.date(...)` returns str on SQLite, date on Postgres. Normalize."""
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    if isinstance(raw, datetime):
        return raw.date()
    return datetime.strptime(str(raw)[:10], "%Y-%m-%d").date()


def _date_range(start: date, end: date) -> list[date]:
    out = []
    d = start
    while d <= end:
        out.append(d)
        d += timedelta(days=1)
    return out


def build_findings_volume(db: Session, mode: Mode, days: int) -> ChartView:
    if mode not in ("type", "competitor"):
        raise ValueError(f"invalid mode: {mode!r}")
    if days not in ALLOWED_DAYS:
        raise ValueError(f"invalid days: {days!r} (allowed: {ALLOWED_DAYS})")

    now = datetime.utcnow()
    since = now - timedelta(days=days)
    today = now.date()
    start_day = (now - timedelta(days=days - 1)).date()
    axis_days = _date_range(start_day, today)

    if mode == "type":
        return _build_by_type(db, since, axis_days, days)
    return _build_by_competitor(db, since, axis_days, days)


def _build_by_type(db: Session, since: datetime, axis_days: list[date], days: int) -> ChartView:
    bucket_expr = func.date(Finding.created_at).label("bucket")
    seg_expr = func.coalesce(Finding.signal_type, "unknown").label("segment")

    rows = (
        db.query(bucket_expr, seg_expr, func.count(Finding.id))
        .filter(Finding.created_at >= since)
        .group_by("bucket", "segment")
        .all()
    )

    # Aggregate by (bucket, segment).
    by_day: dict[date, dict[str, int]] = {d: {} for d in axis_days}
    totals: dict[str, int] = {}
    for raw_bucket, seg, n in rows:
        b = _parse_bucket(raw_bucket)
        if b not in by_day:
            # Row outside the axis (shouldn't happen — `since` matches — but be safe).
            continue
        by_day[b][seg] = by_day[b].get(seg, 0) + int(n)
        totals[seg] = totals.get(seg, 0) + int(n)

    # Legend: keep taxonomy order, but drop types with zero in the window.
    seen_order = [t for t in SIGNAL_TYPE_ORDER if t in totals]
    # Anything unexpected (e.g. a new taxonomy value not yet in SIGNAL_TYPE_ORDER)
    # gets appended alphabetically so it still renders.
    for key in sorted(totals.keys()):
        if key not in seen_order:
            seen_order.append(key)

    segments = [
        Segment(
            key=k,
            label=k,
            color=SIGNAL_TYPE_COLORS.get(k, OTHER_COLOR),
            total=totals[k],
        )
        for k in seen_order
    ]

    bars = _assemble_bars(axis_days, by_day, seen_order)
    y_max = max((b.total for b in bars), default=0)
    return ChartView(
        mode="type",
        days=days,
        bars=bars,
        segments=segments,
        y_max=y_max,
        total_findings=sum(totals.values()),
    )


def _build_by_competitor(db: Session, since: datetime, axis_days: list[date], days: int) -> ChartView:
    # Step 1: top-N competitors in the window.
    top_rows = (
        db.query(Finding.competitor, func.count(Finding.id).label("n"))
        .filter(Finding.created_at >= since)
        .group_by(Finding.competitor)
        .order_by(func.count(Finding.id).desc())
        .limit(TOP_N_COMPETITORS)
        .all()
    )
    top_names = [name for (name, _n) in top_rows if name]
    top_set = set(top_names)

    if not top_names:
        return ChartView(
            mode="competitor",
            days=days,
            bars=[DayBar(bucket=d, total=0, parts=[]) for d in axis_days],
            segments=[],
            y_max=0,
            total_findings=0,
        )

    # Step 2: one pass, bucket non-top competitors into "Other".
    bucket_expr = func.date(Finding.created_at).label("bucket")
    rows = (
        db.query(bucket_expr, Finding.competitor, func.count(Finding.id))
        .filter(Finding.created_at >= since)
        .group_by("bucket", Finding.competitor)
        .all()
    )

    by_day: dict[date, dict[str, int]] = {d: {} for d in axis_days}
    totals: dict[str, int] = {}
    other_members: dict[str, int] = {}  # name → total count, for Other tooltip

    for raw_bucket, competitor, n in rows:
        b = _parse_bucket(raw_bucket)
        if b not in by_day:
            continue
        count = int(n)
        seg = competitor if competitor in top_set else OTHER_LABEL
        by_day[b][seg] = by_day[b].get(seg, 0) + count
        totals[seg] = totals.get(seg, 0) + count
        if seg == OTHER_LABEL and competitor:
            other_members[competitor] = other_members.get(competitor, 0) + count

    # Legend order: top-N by window volume (already sorted DESC via top_rows), then Other last.
    order = [name for name in top_names if name in totals]
    if OTHER_LABEL in totals:
        order.append(OTHER_LABEL)

    segments: list[Segment] = []
    for name in order:
        if name == OTHER_LABEL:
            detail = ", ".join(
                f"{n} ({c})" for n, c in sorted(other_members.items(), key=lambda kv: -kv[1])
            ) or None
            segments.append(Segment(
                key=OTHER_LABEL,
                label=OTHER_LABEL,
                color=OTHER_COLOR,
                total=totals[OTHER_LABEL],
                detail=detail,
            ))
        else:
            segments.append(Segment(
                key=name,
                label=name,
                color=stable_color_for_competitor(name),
                total=totals[name],
            ))

    bars = _assemble_bars(axis_days, by_day, order)
    y_max = max((b.total for b in bars), default=0)
    return ChartView(
        mode="competitor",
        days=days,
        bars=bars,
        segments=segments,
        y_max=y_max,
        total_findings=sum(totals.values()),
    )


def _assemble_bars(
    axis_days: list[date],
    by_day: dict[date, dict[str, int]],
    segment_order: list[str],
) -> list[DayBar]:
    """Build one DayBar per axis day, with `parts` in the same order as the legend.

    Stable ordering across days matters — if segments jitter in stack position
    day-to-day the eye reads it as noise.
    """
    bars: list[DayBar] = []
    for d in axis_days:
        counts = by_day.get(d, {})
        parts = [(seg, counts[seg]) for seg in segment_order if counts.get(seg)]
        total = sum(n for _seg, n in parts)
        bars.append(DayBar(bucket=d, total=total, parts=parts))
    return bars
