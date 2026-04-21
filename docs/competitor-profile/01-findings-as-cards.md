# Spec 01 — Findings as cards on the competitor profile

**Status:** Draft
**Owner:** Simon
**Depends on:** —
**Unblocks:** a single shared "finding" presentation across stream / profile / saved filters; future per-finding actions (open in stream, copy link) without re-styling each surface.

## Purpose

The "Recent findings" table on `/competitors/<id>` (currently `app/templates/competitor_profile.html` lines 193–235) shows the same underlying objects as the stream feed but renders them as a metadata-heavy table — provider, score, dates, title — with a click-to-expand row that dumps URL, hash, run id, and a `<pre>` of the scraped body.

Replace it with the same card shape used in the stream (`app/templates/_stream_card.html`), so a finding looks the same wherever it appears. Add an inline expand on the card to reveal the full scraped `content`. Drop the actions that don't make sense in this read-only context (ratings, state tracking, swipe).

## Non-goals

- Changing the stream card itself (no new fields, no new behavior on `/stream`).
- Wiring rating / pin / dismiss / snooze on the competitor profile. This page is read-only — the stream is where signal-log events get generated.
- Filtering, sorting, or paginating the competitor's findings beyond what's there today. Scope is presentation.
- Touching the `Momentum` table or the `Current strategy review` panel.
- Migrating data, adding columns, or changing the `Finding` model.
- Mobile swipe-to-rate. Not applicable here — no rating actions.

## Design principles

1. **One card template, two surfaces.** Don't fork `_stream_card.html`. Make it parametric so the stream renders the full version and the competitor profile renders a stripped read-only version. A second template would diverge the moment either surface gets a new field.
2. **Expand reveals what the table's detail row already showed.** The current expand surfaces full `content` plus a few provenance fields. Keep that — just present it inside the card, not in a sibling `<tr>`.
3. **Drop redundant fields.** On the profile page the competitor name is the page title; don't repeat it on every card. Provider / score / topic / hash / run id move into the expanded panel where they belong (provenance is useful but not glanceable).
4. **No new JS framework.** Reuse the existing in-template toggle pattern (the one already in `competitor_profile.html` for the table detail row). Plain `addEventListener`, `display: none`, caret rotation. No HTMX round-trip — content is already in the DOM.
5. **CSS lives where the cards live.** All card styling stays in the existing stream stylesheet (`app/static/...`) so a future change touches one file. The expand panel gets its own class (`.signal-card-expanded`) in the same file.

## Where it lives

- Surface: existing `/competitors/<id>` route (`app/ui.py::competitor_profile` — confirm name when implementing). The "Recent findings" panel.
- Templates:
  - `app/templates/_stream_card.html` — extended with new optional flags (see below). Single source of truth.
  - `app/templates/competitor_profile.html` — replace the table block at lines 193–235 with a `.stream-list`-style grid that loops `_stream_card.html` with the read-only flags set.
- No new routes. The full `content` is already loaded by the existing query that builds the page; nothing to fetch on expand.

## Card parameterization

`_stream_card.html` gains three optional flags, all defaulting to the current stream behavior so `/stream` is unchanged:

| flag                | default | effect when off (profile usage)                                       |
| ------------------- | ------- | --------------------------------------------------------------------- |
| `show_rating`       | `True`  | Hide the 👍 / 👎 buttons. No `card-actions` block rendered.           |
| `show_competitor`   | `True`  | Hide the competitor name in the head. (Page title carries it.)        |
| `expandable`        | `False` | Render an expand caret in the head + a hidden `.signal-card-expanded` block beneath the body. |

The stream call site passes nothing — current behavior preserved. The profile call site passes `show_rating=False, show_competitor=False, expandable=True`.

The `state-new` / `new-dot` indicator: keep it on the profile too. It's informational ("we picked this up since you last looked") and doesn't depend on the rating subsystem. If it ever feels noisy on the profile, gate it behind a fourth flag — not now.

## Expanded panel

Rendered only when `expandable=True`. Hidden by default. Caret in the card head toggles it (`›` collapsed, `⌄` open) using the same JS pattern already in `competitor_profile.html` lines 240–250 — generalize it to operate on `[data-expandable]` cards instead of `.finding-row` table rows.

Contents, in order:

1. **Provenance row** — small muted text, flex-wrap, single line at desktop width. Fields:
   - `URL` — clickable, mono, truncated to 90 chars (existing behavior).
   - `Topic` — `f.topic` or `—`.
   - `Provider` — `f.search_provider` or `—`.
   - `Score` — `'%.2f' % f.score` or `—`.
   - `Matched keyword` — `f.matched_keyword` or `—` (currently not shown anywhere on this page; small win for free).
   - `Hash` — `f.hash[:12]`, mono.
   - `Run` — link to `/runs/{f.run_id}` if present.
   - `Length` — `len(f.content)` chars.
2. **Full content** — `<pre>` with the same constraints used today: `white-space: pre-wrap; word-break: break-word; max-height: 400px; overflow: auto`. Fallback text identical to today: `"(no content stored — this finding arrived as snippet-only)"`.

No edit affordances. No "view in stream" link in v1 (noted under *What this unblocks*).

## Layout

The "Recent findings" panel keeps its existing `<div class="panel">` shell and `<div class="panel-header"><h2>Recent findings</h2>` plus the `provider_breakdown` pills (those stay — they're a panel-level summary, not a per-finding thing).

The `<table>` is replaced with:

```jinja
<div class="stream-list">
  {% for f in findings %}
    {% include "_stream_card.html" with context %}
    {# call site sets: show_rating=False, show_competitor=False, expandable=True #}
  {% else %}
    <p class="muted">No findings on record.</p>
  {% endfor %}
</div>
```

(Exact include/with mechanics depend on the Jinja config — pass the flags via `set` before `include`, or convert the partial to a macro if cleaner. Implementer's call.)

`.stream-list` is the same 2-column grid the stream uses (collapses to 1-col on mobile). On the competitor profile this means findings cards sit at the same width and rhythm as on `/stream`. That is the alignment the user is asking for.

## Empty state

`No findings on record.` Keep the existing copy. Render as a single `<p class="muted">` inside the panel body, not a card.

## What gets removed

- The `<table>` / `<thead>` / `<tbody>` block (lines 193–235).
- The per-row `<tr id="finding-{id}-detail">` sibling row.
- The `.finding-row` click handler (lines 240–250). Replaced by the generalized `[data-expandable]` handler living next to the new card behavior.

The provider-breakdown pills in the panel header stay exactly as-is.

## Accessibility

- Caret button is a `<button type="button">` (not a styled `<span>`), with `aria-expanded` reflecting state and `aria-controls` pointing at the `.signal-card-expanded` block id.
- Expanded block id: `card-{f.id}-expanded`.
- Keyboard: caret button is tabbable, Enter/Space toggles. The card itself is not a clickable region — only the caret and the title link are interactive. (The current table makes the whole row clickable, which is convenient for a dense table but wrong for cards where the title is already a link.)

## CSS additions

In the stylesheet that owns `.signal-card`:

```css
.signal-card-head .expand-toggle {
  background: none;
  border: 0;
  color: var(--text-muted);
  font-size: 14px;
  line-height: 1;
  padding: 2px 4px;
  cursor: pointer;
}
.signal-card-head .expand-toggle[aria-expanded="true"] { color: var(--text); }

.signal-card-expanded {
  border-top: 1px solid var(--border);
  margin-top: 8px;
  padding-top: 10px;
  display: none;
}
.signal-card[data-expanded="true"] .signal-card-expanded { display: block; }

.signal-card-expanded .provenance {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  font-size: 11px;
  color: var(--text-muted);
  margin-bottom: 8px;
}
.signal-card-expanded pre {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 400px;
  overflow: auto;
  font-size: 12px;
  line-height: 1.5;
}
```

Tune to whatever the stream card already uses for borders / spacing — match, don't reinvent.

## Performance

The page already loads all findings + their `content` for the current table (the detail row puts content in the DOM up-front). Card swap is a presentation change; same query, same payload, same byte count within rounding. No regression.

If `content` is large enough that pre-loading every body becomes a real cost (it isn't today), the follow-up is to load expanded content lazily via HTMX `hx-get="/partials/finding/{id}/content"`. Out of scope.

## Testing

- Render `/competitors/<seeded-id>` on a dev DB with at least one finding that has `content`, one that doesn't, one with `summary`, one with neither (falls back to `content[:280]` per existing card logic). Eyeball: same visual rhythm as `/stream`, no rating buttons, no competitor name on the cards, expand toggle works.
- Render `/stream` on the same DB. Confirm zero visual change vs. main — the new flags default to current behavior.
- Click a card title link. Should open the source URL in a new tab and **not** trigger expand.
- Tab through a card. Caret button is reachable, Enter toggles, `aria-expanded` flips.
- Empty competitor (no findings). Renders `No findings on record.` and no broken grid.
- Snapshot test on `_stream_card.html` covering both flag combinations (default-on, profile-off) — protects against accidental divergence the next time the card gains a field.

## Acceptance criteria

1. `_stream_card.html` accepts `show_rating`, `show_competitor`, `expandable` with the defaults specified above. `/stream` renders byte-identical to main when no flags are passed.
2. `competitor_profile.html` renders findings inside `.stream-list` via the shared partial with the three read-only flags set. The old `<table>` block and its detail rows are removed.
3. Expand caret toggles a panel containing the provenance row and the full `<pre>` content. `aria-expanded` reflects state.
4. The provider-breakdown pills in the panel header are unchanged.
5. The empty state renders a single muted line, not an empty grid.
6. No new routes. No new JS dependencies. No DB changes.
7. Snapshot tests cover both card variants.

## What this unblocks

- A "View in stream" link from any card (including the profile variant), once the stream supports a `?finding=<id>` deeplink.
- Reusing the same partial on a future "saved filter results" page or on the search/admin views — same flag pattern applies.
- Lazy content loading (described under *Performance*) becomes a one-line template change if findings ever bloat the page weight.
