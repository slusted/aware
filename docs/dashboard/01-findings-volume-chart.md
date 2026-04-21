# Spec 01 — Findings Volume Chart

**Status:** Draft
**Owner:** Simon
**Depends on:** —
**Unblocks:** richer dashboard analytics (per-competitor trend cards, materiality breakdown)

## Purpose

Give the `/dashboard` a single, glanceable daily stacked-bar chart of how many findings have been established over time. Two toggleable groupings:

1. **By type** — each stack segment is a `signal_type`.
2. **By competitor** — each stack segment is a competitor name.

Total bar height is the same in both modes (total findings that day). Only the segmentation changes.

## Non-goals

- Hour-level granularity. Day buckets only.
- Filtering by materiality / source / keyword inside the chart. The stream has filters — the dashboard chart is a volume overview, not a query tool.
- Per-user personalization (the ranker does that elsewhere). This chart shows raw volume of what the system established, not what the current user engaged with.
- A charting-library dependency. Render inline SVG from Jinja (same technique as the existing inline-SVG snippets in `app/templates/competitor_profile.html`, but the chart shape is a stacked bar — one `<rect>` per segment per day — not a line).
- Editing / correcting / backfilling findings. Read-only visualization.

## Design principles

1. **One query per render.** A single indexed aggregation over `findings` produces everything the template needs. No N+1. No per-bucket loops in Python.
2. **Server-rendered SVG.** Jinja loop emits `<rect>` elements. No JS chart lib, no client-side data payload. HTMX swaps the fragment on mode / window change.
3. **Stable colors.** `signal_type` maps to a fixed palette (taxonomy is closed). `competitor` maps via a deterministic hash → palette index so the same competitor is the same color across renders.
4. **Bounded cardinality.** Competitor-mode stacks the top N competitors by volume over the selected window; everything else collapses to a single `Other` segment. Keeps the legend readable and the rendered SVG cheap.
5. **No new tables.** Everything derives from `findings.created_at`, `findings.signal_type`, `findings.competitor`. Read-path only.

## Where it lives

- Surface: the existing `/dashboard` route (`app/ui.py::dashboard`), rendered in `app/templates/dashboard.html`.
- Placement: a new `<div class="panel">` titled **Findings volume**, inserted between the `stat-grid` and the `Recent runs` table.
- Fragment endpoint: `GET /partials/findings-volume` (new, in `app/ui.py`) returns the chart panel body. The dashboard template includes it on load; the mode / window controls re-fetch it via HTMX `hx-get` with `hx-swap="outerHTML"` on the panel body.

## Data source

All fields already on `findings`:

- `created_at` — bucketing dimension.
- `signal_type` — nullable. `NULL` renders as `unknown`.
- `competitor` — non-null on every row.

No migration. No new indexes strictly required for v1 (existing indexes on `created_at`, `signal_type`, `competitor` cover the group-bys for the windows we care about — see *Performance*).

## Query

One query per render, parameterized by `mode` ∈ {`type`, `competitor`} and `days` ∈ {7, 30, 90}.

### Mode = type

```sql
SELECT
  date(created_at)                     AS bucket,
  COALESCE(signal_type, 'unknown')     AS segment,
  COUNT(*)                             AS n
FROM findings
WHERE created_at >= :since
GROUP BY bucket, segment
ORDER BY bucket ASC, segment ASC;
```

### Mode = competitor

Two-step, same transaction:

1. Pick the top N competitors in the window (N = 10 default):

    ```sql
    SELECT competitor
    FROM findings
    WHERE created_at >= :since
    GROUP BY competitor
    ORDER BY COUNT(*) DESC
    LIMIT :top_n;
    ```

2. Aggregate, bucketing anything not in the top set to `Other`:

    ```sql
    SELECT
      date(created_at) AS bucket,
      CASE WHEN competitor IN (:top_set) THEN competitor ELSE 'Other' END AS segment,
      COUNT(*) AS n
    FROM findings
    WHERE created_at >= :since
    GROUP BY bucket, segment
    ORDER BY bucket ASC, segment ASC;
    ```

Empty top set → skip step 2 and render the empty state.

### Bucketing

- `date(created_at)` in **SQLite** returns the UTC date. `findings.created_at` is stored as naive UTC (via `datetime.utcnow`), so bucket labels are UTC days. Good enough for v1; revisit if users complain about midnight-crossing misalignment (open question below).
- Days with zero findings must still appear on the x-axis. Backfill a `bucket → 0` row in Python after the query so the chart has a continuous axis (no gaps collapsed).

## Shape returned to the template

```python
@dataclass
class Segment:
    key: str            # "new_hire", "Indeed", "Other", "unknown"
    color: str          # "#5aa3ff", resolved at build time
    total: int          # sum across the window, for legend ordering

@dataclass
class DayBar:
    date: date
    total: int
    parts: list[tuple[str, int]]   # [(segment_key, count)] — order matches legend order

@dataclass
class ChartView:
    mode: Literal["type", "competitor"]
    days: int
    bars: list[DayBar]        # length == days, chronological
    segments: list[Segment]   # legend, ordered by total DESC
    y_max: int                # for SVG scaling
    total_findings: int
```

Builder lives in `app/dashboard_chart.py::build_findings_volume(db, mode, days)`. Pure; no I/O beyond the DB session.

## Color mapping

### signal_type (fixed palette — taxonomy is closed)

| signal_type       | color     |
| ----------------- | --------- |
| `news`            | `#6b7280` (slate) |
| `price_change`    | `#f59e0b` (amber) |
| `new_hire`        | `#10b981` (green) |
| `product_launch`  | `#3b82f6` (blue)  |
| `messaging_shift` | `#8b5cf6` (violet)|
| `funding`         | `#ec4899` (pink)  |
| `integration`     | `#14b8a6` (teal)  |
| `voc_mention`     | `#f43f5e` (rose)  |
| `momentum_point`  | `#eab308` (yellow)|
| `other`           | `#94a3b8` (gray)  |
| `unknown` (null)  | `#cbd5e1` (lightest gray, visually de-emphasized) |

Lives as a module-level dict in `app/dashboard_chart.py`. Changing the palette is a one-line edit.

### competitor (hash → palette)

- Palette: a fixed ordered list of ~12 distinct hues (separate from the signal-type palette to avoid confusion when toggling).
- Assignment: `palette[ stable_hash(name) % len(palette) ]`. `stable_hash` must not depend on Python's process-salted `hash()` — use `zlib.crc32(name.encode())` or equivalent so colors are consistent across restarts.
- `Other` is always a fixed muted gray (`#94a3b8`), never pulled from the rotating palette.
- Collisions (two competitors same color) are tolerated — legend shows the name next to the swatch; the chart is not decoding identity from color alone.

## Top-N cap (competitor mode)

- `TOP_N = 10`.
- Document the choice here — retune against real data without a migration. Bumping to 15 is a constant change.
- The `Other` bucket is *always* rendered last in the stack (drawn at the top of the bar) and last in the legend, with its own gray swatch and a tooltip showing the count of competitors it hides.

## Rendering (SVG)

Inline SVG in the fragment template `app/templates/_findings_volume.html`. Sized responsively (`width="100%"`, fixed `viewBox`).

Approximate layout (pseudocode):

```jinja
<svg viewBox="0 0 {{ chart.days * BAR_STEP }} {{ CHART_HEIGHT }}" ...>
  {% for bar in chart.bars %}
    {% set x = loop.index0 * BAR_STEP %}
    {% set y_cursor = namespace(v = CHART_HEIGHT - AXIS_PAD) %}
    {% for key, n in bar.parts %}
      {% set h = (n / chart.y_max) * (CHART_HEIGHT - AXIS_PAD - TOP_PAD) %}
      {% set y_cursor.v = y_cursor.v - h %}
      <rect x="{{ x }}" y="{{ y_cursor.v }}" width="{{ BAR_W }}" height="{{ h }}"
            fill="{{ segment_color[key] }}">
        <title>{{ bar.date }} · {{ key }}: {{ n }}</title>
      </rect>
    {% endfor %}
  {% endfor %}
  {# x-axis ticks — every 7th day labeled. y-axis ticks — y_max + midpoint. #}
</svg>
```

Constants (tunable in the template / builder):

- `BAR_STEP = 14` (px at viewBox scale)
- `BAR_W = 10`
- `CHART_HEIGHT = 180`
- `AXIS_PAD = 20` (bottom, for date labels)
- `TOP_PAD = 6`

Hover: the native `<title>` tooltip is enough for v1 — no custom tooltip JS.

Segment ordering inside a bar: stable across bars (follow legend order, `Other` last). This is important — if segment order jitters day-to-day the eye reads it as noise.

## Controls

Rendered inside the panel header:

```html
<div class="chart-controls">
  <div class="segmented">
    <button hx-get="/partials/findings-volume?mode=type&days={{ days }}" ...>By type</button>
    <button hx-get="/partials/findings-volume?mode=competitor&days={{ days }}" ...>By competitor</button>
  </div>
  <div class="segmented">
    <button hx-get="/partials/findings-volume?mode={{ mode }}&days=7"  ...>7d</button>
    <button hx-get="/partials/findings-volume?mode={{ mode }}&days=30" ...>30d</button>
    <button hx-get="/partials/findings-volume?mode={{ mode }}&days=90" ...>90d</button>
  </div>
</div>
```

- `hx-target` is the panel body; `hx-swap="outerHTML"`.
- Active-state styling on the current mode / window (already supported by the `.pill` / segmented button styles used elsewhere — pick whichever is in use on the stream filters).
- Defaults: `mode=type`, `days=30`. Persisted per-user via a short-lived cookie (`cw_chart_mode`, `cw_chart_days`) — no new DB row. Not critical; skip if it blows the scope.

## Legend

Under the chart. Ordered by `segments[*].total` DESC. Each entry: swatch, label, window total in parentheses.

Legend labels:

- Mode = type: the taxonomy string verbatim (`new_hire`, `product_launch`, ...).
- Mode = competitor: competitor name. `Other` gets a title-tooltip listing the hidden competitor names + their individual counts (built into the `Segment` dataclass as an optional `detail` field).

## Empty state

If `total_findings == 0` in the window: render the panel with a muted `<p>` — "No findings in the last {days} days. Trigger a scan above." Skip the SVG entirely. No zero-height bars.

## Interaction (out of scope but named)

Click a segment → stream filtered to that competitor / type on that day. Not v1 — the URL shape for stream filters can be whatever `app/templates/stream.html` already supports; wiring it up is a one-line anchor once we know we want it. Noted here so the `Segment` / `DayBar` shape is not regretted later.

## Performance expectations

- Window ≤ 90 days. At 1000 findings/day (very generous), `GROUP BY bucket, segment` over 90k rows on SQLite with `idx_findings_created_at` completes in well under 100ms.
- Fragment render: single query + one Jinja render, target ≤ 150ms p95. Measure with the existing usage/latency instrumentation if it's there; otherwise eyeball it for v1.
- If we ever scale past ~1M rows in the window, the fix is a small `findings_daily_rollup` table populated by the nightly job — same shape as this spec returns. Not worth building preemptively.

## Testing

- Unit: `build_findings_volume` with a seeded set of findings across known dates / types / competitors returns the expected bars, segments, and `y_max`. Include a day with zero findings (proves axis backfill).
- Unit: top-N cap. Seed 15 competitors, `TOP_N=10`, assert segment list has 11 entries (top 10 + `Other`) and `Other.total` equals the hidden five.
- Unit: `stable_hash` — same name → same color across two invocations of the function (guard against `hash()` salt regressions).
- Smoke: hit `/partials/findings-volume?mode=type&days=30` on a seeded dev DB, eyeball the SVG in the browser, toggle to competitor mode, toggle windows.

## Open questions

1. **Timezone.** UTC day buckets are simple and match the existing `datetime.utcnow` storage convention, but a user in PT seeing "Wednesday" findings actually aggregated from Tue 17:00 → Wed 17:00 local can be confusing. Options: (a) keep UTC and label the axis "UTC" in small text, (b) bucket in the user's TZ using a stored preference, (c) bucket in the server's local TZ. Proposal: (a) for v1 — one line of label text, no query change. Revisit when the app gets a real TZ preference.
2. **Does `Other` click-through make sense?** For v1 it's non-interactive. If we wire up click-through (post-v1), `Other` probably shouldn't be clickable — the stream has no "not in top 10" filter. Leave it static.
3. **Should the chart scope by user / team?** Today it's global (all findings). If multi-tenant data lands later, scope by whatever bound `/dashboard` itself uses. Not a decision this spec has to make — just don't bake `WHERE user_id` assumptions in yet.

## Acceptance criteria

1. `app/dashboard_chart.py` exposes `build_findings_volume(db, mode, days) -> ChartView`. Unit tests cover both modes, top-N cap, zero-day backfill, stable colors.
2. `GET /partials/findings-volume?mode=&days=` returns the rendered fragment. Invalid `mode` → 400. Invalid `days` (not in {7, 30, 90}) → 400.
3. `app/templates/_findings_volume.html` renders the SVG and legend from a `ChartView`.
4. `app/templates/dashboard.html` includes the panel between the `stat-grid` and `Recent runs` table, loading the fragment on page load.
5. Toggling mode and window swaps the fragment via HTMX with no full page reload. Active-button state reflects the current view.
6. Empty-window state renders the muted "No findings…" text; no broken / zero-height SVG.
7. Hover over a segment shows a native tooltip `"YYYY-MM-DD · <segment>: <n>"`.
8. Same competitor renders the same color across a restart (`stable_hash` test).
9. Manual smoke on a seeded dev DB: both modes, all three windows, a zero-findings day visible on the axis, legend totals sum to the day's `total`.

## What this unblocks

A per-competitor trend card on `/competitors/<id>` can reuse `build_findings_volume` with a `competitor=` filter added (one-line predicate). A materiality-weighted version (`SUM(materiality)` instead of `COUNT(*)`) is a second function in the same module. Both are cheap follow-ups once this spec ships.
