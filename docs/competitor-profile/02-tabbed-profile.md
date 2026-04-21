# Spec 02 — Tabbed competitor profile

**Status:** Draft
**Owner:** Simon
**Depends on:** 01 (findings-as-cards — the Findings tab reuses `_stream_card.html` via the flags that spec added).
**Unblocks:** adding more surfaces to the profile (e.g. "Context", "Peers") without further stacking panels; per-tab deep-links from the stream or email digests.

## Purpose

`app/templates/competitor_profile.html` currently stacks four panels down one scroll column: Current strategy review, Momentum, Prior reviews, Recent findings. On a seeded competitor the page is long enough that the findings list (the densest content) sits far below the fold, and Momentum — which is only occasionally useful — takes up prime real estate above it.

Split the profile into three tabs so each surface has the full content width, and so switching is one click instead of a scroll:

1. **Review** (default) — the `Current strategy review` panel, plus `Prior reviews` underneath it. These belong together: prior reviews are history for the same artifact.
2. **Momentum** — the momentum table as-is.
3. **Findings** — the findings grid + provider breakdown pills.

The page header (name chip row, category/source chips, threat angle) and the action buttons (Edit / Regenerate review / Full scan) stay above the tab bar — they apply to the competitor, not to a single tab.

## Non-goals

- Changing what any tab renders. Content per tab is unchanged from today. This is purely a layout/navigation change.
- Server-side tab routing. One route, one query, one template — tabs are a client-side reveal.
- Lazy-loading tab contents. Same query, same payload as today. If the findings or momentum payloads ever become expensive, revisit with HTMX partials; not now.
- Persisting the active tab across sessions. The hash in the URL is enough (see *Deep-linking*).
- Touching the stream, admin, or other surfaces.

## Design principles

1. **Tabs are panels you switch between, not new pages.** One template renders all three tab bodies; JS toggles `[hidden]`. No route changes, no extra fetch per tab switch.
2. **URL hash drives state.** `#review` (or no hash) shows Review, `#momentum` shows Momentum, `#findings` shows Findings. Back/forward in the browser flips tabs. Shareable links land on the right tab.
3. **No new JS framework.** ~30 lines of vanilla JS at the bottom of the template, same style as the existing scan-button state machine. Tabs are one `click` handler + one `hashchange` handler.
4. **CSS in `style.css`, not inline.** One `.tabs` / `.tab` / `.tab-panel` block. Matches the rest of the codebase's style.css-first approach.
5. **Progressive enhancement floor, not ceiling.** With JS off, all three panels render stacked (the current layout). JS hides the inactive ones on load. This is a nice side-effect of doing reveal-via-`[hidden]` — the server never has to know which tab is active.

## Where it lives

- `app/templates/competitor_profile.html` — wraps the existing three panel groups in `<section class="tab-panel" id="tab-review|momentum|findings">`. Adds a `<nav class="tabs">` above them. Extends the existing `<script>` block with tab switching.
- `app/static/style.css` — adds `.tabs`, `.tab`, `.tab.active`, `.tab-panel[hidden]` rules. Bump the `?v=` cache-buster on the stylesheet `<link>` in `base.html`.
- `app/ui.py::competitor_profile` — no change.
- No new routes, no new partials, no new migrations, no JS dependencies.

## Tab bar

```html
<nav class="tabs" role="tablist" aria-label="Competitor views">
  <button role="tab" class="tab active" data-tab="review"
          aria-selected="true" aria-controls="tab-review">Review</button>
  <button role="tab" class="tab" data-tab="momentum"
          aria-selected="false" aria-controls="tab-momentum">Momentum</button>
  <button role="tab" class="tab" data-tab="findings"
          aria-selected="false" aria-controls="tab-findings">Findings</button>
</nav>
```

Sits between the header block (chips + threat_angle) and the first `.panel`. No badge counts on tabs in v1 — we can add `(12)` on Findings if it proves useful, but start simple.

## Tab panels

Each panel wraps existing markup. No content change.

- `#tab-review` — wraps the current `Current strategy review` `.panel` + the `Prior reviews` `.panel` (the `{% if history %}` block). Rationale above.
- `#tab-momentum` — wraps the current `Momentum` `.panel` (and its three empty-state variants: populated / identifiers-but-no-data / no-identifiers).
- `#tab-findings` — wraps the current `Recent findings` `.panel`.

Panel structure:

```html
<section class="tab-panel" id="tab-review" role="tabpanel" aria-labelledby="...">
  ... existing panels unchanged ...
</section>
```

`hidden` is applied by JS on load based on the URL hash. First paint is "all panels visible" to keep the no-JS fallback sensible; the JS runs synchronously before paint is visible in practice (script tag stays at the end of `{% block content %}`, so elements exist when it runs).

## URL hash & default tab

- Default: `#review` (or no hash). Review is the user's primary artifact on this page.
- Hashes the JS recognizes: `#review`, `#momentum`, `#findings`. Anything else → fall back to Review.
- Clicking a tab sets `location.hash` with `history.replaceState` (so rapid tab-flipping doesn't pollute back-stack). `hashchange` listener reacts to manual URL edits + browser back/forward.
- **Exception:** if `location.hash` was empty on load, we don't write one — landing on `/competitors/42` stays on `/competitors/42`, not `/competitors/42#review`. Prevents a cosmetic churn on first render.

## Interaction with existing scripts

The template already has:
- The regenerate-review / full-scan / cancel state machine.
- The `[data-expandable]` card toggle.
- Marked.js for rendering `latest.body_md`.
- `showPrior(id)` for expanding prior-review rows.

None of these care which tab is visible — they operate on elements by id or selector and those elements still exist in the DOM. Specifically:

- `renderReview()` runs once on load and populates `#review-body`. Works whether Review is the visible tab or not.
- `showPrior` is reachable only when the Review tab is visible (since Prior reviews sit in that tab). No change needed.
- `tickScanState` may flip to "running" while the user is on the Findings tab. That's fine — the button lives in the header above the tab bar, not inside a tab panel.
- `[data-expandable]` lives on finding cards inside the Findings tab. Toggling their `aria-expanded` while the tab is hidden is harmless — they reveal correctly when the tab becomes visible.

## CSS

Add to `app/static/style.css`, near the other nav-y rules (after `.nav-item` or near `.panel-header`). Use existing CSS variables (`--border`, `--text`, `--text-dim`, `--bg-subtle`) — match, don't reinvent.

```css
.tabs {
  display: flex;
  gap: 2px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 20px;
}
.tab {
  background: none;
  border: 0;
  border-bottom: 2px solid transparent;
  color: var(--text-dim);
  cursor: pointer;
  font: inherit;
  font-size: 13px;
  font-weight: 500;
  padding: 10px 14px;
  margin-bottom: -1px; /* overlap the container border */
}
.tab:hover { color: var(--text); }
.tab.active {
  color: var(--text);
  border-bottom-color: var(--text);
}
.tab:focus-visible {
  outline: 2px solid var(--accent, #4a7cff);
  outline-offset: -2px;
  border-radius: 4px 4px 0 0;
}
.tab-panel[hidden] { display: none; }
```

Bump the cache-buster in `app/templates/base.html` from `?v=responsive-2` to `?v=tabs-1` so existing sessions pick up the new rules.

## JS (goes at the end of the template's `<script>` block)

```js
(function initTabs() {
  var TABS = ['review', 'momentum', 'findings'];
  var buttons = document.querySelectorAll('.tabs .tab');
  var panels  = document.querySelectorAll('.tab-panel');

  function show(name) {
    if (TABS.indexOf(name) === -1) name = 'review';
    buttons.forEach(function (b) {
      var active = b.dataset.tab === name;
      b.classList.toggle('active', active);
      b.setAttribute('aria-selected', active ? 'true' : 'false');
    });
    panels.forEach(function (p) {
      if (p.id === 'tab-' + name) p.removeAttribute('hidden');
      else p.setAttribute('hidden', '');
    });
  }

  buttons.forEach(function (b) {
    b.addEventListener('click', function () {
      var name = b.dataset.tab;
      show(name);
      // Only touch the URL if we're not already on the default-no-hash state.
      history.replaceState(null, '', '#' + name);
    });
  });

  window.addEventListener('hashchange', function () {
    show((location.hash || '').replace('#', ''));
  });

  show((location.hash || '').replace('#', ''));
})();
```

## Accessibility

- Buttons use `role="tab"` inside a `role="tablist"` container. Panels use `role="tabpanel"` with `aria-labelledby` pointing at the controlling tab's id.
- `aria-selected` reflects the active tab. Inactive panels get `hidden`, which removes them from the a11y tree.
- Keyboard: tab-key reaches each button naturally; Enter/Space activates. Arrow-key navigation between tabs is a nice-to-have but not shipped in v1 — revisit if the tab bar ever grows past three items.
- Screen reader users landing on `#findings` still get a meaningful experience: the findings panel heading ("Recent findings") is the first thing inside that panel.

## Responsive

- At the current breakpoints the tab bar fits comfortably on one row (three short labels, ~300px total).
- On narrow viewports (<480px) the existing sidebar collapses; tabs will naturally wrap if they ever exceed width. No custom stacking rule needed.
- Mobile tested sizes: 375px, 768px, desktop.

## Testing

- Open `/competitors/<seeded-id>`. Defaults to Review. All three panels exist in DOM; only Review content is visible.
- Click Momentum. URL gets `#momentum`. Review + Findings panels hidden. Momentum panel visible.
- Click Findings. URL gets `#findings`. Cards render and expand correctly.
- Reload with `#momentum` in the URL. Momentum tab is active on load.
- Enter `/competitors/42#bogus`. Falls back to Review.
- Browser back after clicking through tabs restores prior tab (note: we use `replaceState`, so back goes to the previous *page*, not the previous tab — that's intentional; deep-linked tabs are shareable but flipping tabs shouldn't pollute history).
- Regenerate review while on Findings tab. Scan button updates correctly in the header. Page reloads on success and lands on the original tab (preserved by the hash).
- Visual: tab borders align cleanly with the top of the first panel; active tab underline is the text color; hover brightens inactive tabs. Match the look of other flat/minimal UI elements on the site.

## Acceptance criteria

1. `/competitors/<id>` renders a tab bar between the header chips and the first content panel.
2. Three tabs: Review (default), Momentum, Findings. Each wraps existing content unchanged.
3. URL hash drives + reflects the active tab. `#bogus` → Review. Empty hash → Review, URL unchanged.
4. No route changes, no new server work, no new JS dependencies.
5. All existing behavior on the page (regenerate, scan, findings expand, prior review expand, marked rendering) still works identically.
6. CSS cache-buster bumped so the new rules load without a hard refresh.
7. Keyboard and screen-reader affordances: `role="tab"`/`tablist`/`tabpanel`, `aria-selected`, `aria-controls`, `aria-labelledby` set correctly.

## What this unblocks

- Adding more tabs later (Context, Peers, Pricing) without redesigning the page.
- Per-tab deep links from email digests or stream cards (`/competitors/42#findings` is now meaningful).
- If we ever need to lazy-load Momentum or Findings, the tab boundary is the natural split point for HTMX `hx-get` on tab click.
