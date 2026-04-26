# Design system

Top-level design doc for Competitor Watch. Lives alongside the per-feature specs in `docs/<feature>/` but is intentionally cross-cutting: tokens, theming, type, motion. New entries are appended as numbered sections so the doc grows by accretion, not by rewrite.

The styling philosophy is captured at the top of [style.css](app/static/style.css:1): Linear-inspired, monochrome, tight density, one accent, subtle borders, Inter everywhere, 6–8px radii, no gratuitous shadows. Don't litigate that here — anything below assumes it.

---

## 01 — Light mode

**Status:** Draft
**Owner:** Simon
**Depends on:** —
**Unblocks:** future theming work (high-contrast, brand themes), respects-OS-pref ergonomics

### Purpose

Add an opt-in light theme to the app, toggled by the user and persisted across sessions. The dark theme stays the default.

The work is *mostly* a token cleanup pass. The stylesheet at [app/static/style.css](app/static/style.css) is 2,289 lines and already routes most colors through the `:root` token block at [style.css:5-36](app/static/style.css:5). A clean light theme is feasible only because that scaffolding exists — but ~86 hardcoded color literals scattered through the file bypass it. Those need tokenizing first, otherwise light mode will have dark splotches and broken contrast.

### Non-goals

- **Auto-switching from `prefers-color-scheme`.** v1 is a manual toggle. We can wire OS detection later; doing it first masks bugs because most devs are already on dark.
- **Per-component dark/light overrides.** Everything routes through CSS variables. Components don't know which theme they're in.
- **A second-pass redesign.** Same layout, same density, same components. Only the palette swaps.
- **Theming the email templates** (`docs/chat/02-scheduled-questions.md`). Email is its own surface, separate concern.
- **Theming inline SVGs in templates.** They use `currentColor`/`stroke="currentColor"` already — they ride the text token automatically. The few that don't are listed in the audit below.
- **A design-tokens build pipeline** (Style Dictionary etc). Plain CSS custom properties are fine at this size.

### Design principles

1. **Semantic tokens, not palette tokens.** Rename `--bg`, `--bg-subtle`, `--bg-raised` → `--surface-0`, `--surface-1`, `--surface-2` (or similar). Same for text. The current names describe the dark-mode look (`subtle` is darker than `bg`); in light mode "subtle" makes no sense. Semantic names theme cleanly.
2. **One toggle, one source of truth.** `data-theme="dark|light"` on `<html>`. Both palettes are full overrides under `:root[data-theme="light"]`. No `@media (prefers-color-scheme)` for v1, no per-component theme checks.
3. **No theme-aware JS.** The toggle flips an attribute and writes a cookie. The server reads the cookie on render to set the initial attribute (avoids a flash on load). Everything else is CSS.
4. **Status colors stay recognizable across themes.** ok=green, warn=amber, err=red, info=blue. The *base hue* is shared; only the soft/tint variants change to maintain contrast on each surface.
5. **Tints are tokenized, not literal.** Every `rgba(<status>, 0.08)` becomes `--ok-soft` etc. Light mode redefines those tokens; components don't change.

### Audit — what's hardcoded today

86 color literals, found by grepping `#[0-9a-fA-F]{3,6}|rgba?\(` against [style.css](app/static/style.css). Categorized:

| Category | Lines (approx) | What's there | Cleanup |
| --- | --- | --- | --- |
| **Status tints** (pill / btn-danger / chat-tool / alerts) | 372, 417–420, 614, 1141–1192 (also at 1173–1192, 2183–2250), 1353–1354 | `rgba(<ok\|warn\|err\|info>, 0.04–0.12)` repeated by hand | Add `--ok-soft`, `--ok-soft-strong`, `--ok-border-soft` (× ok/warn/err/info). Components reference tokens. |
| **Type-color palette** (signal_type badges / chips / event-material) | 1141–1192 | 8 fixed hues × 2 alphas (0.08 / 0.12 / 0.35 / 0.45). Identity colors (funding=green, integration=teal, …). | Hoist to `:root` as `--type-funding`, `--type-funding-soft`, `--type-funding-border`. Light mode tunes the alphas; the *hue* is shared with dark. |
| **Backdrop overlays** (sticky topbar, landing nav, landing radial) | 173, 1729, 1738 | `rgba(12,13,16, …)` — duplicates of `--bg` with alpha | `--bg-overlay`, `--bg-overlay-strong`. Landing radial gets `--bg-glow` with theme-specific tint. |
| **Brand gradient end-stop** | 94, 1762, 1815 | `#b8a8ff` (the violet end of the brand gradient) | `--accent-2`. Light mode picks a deeper end so the gradient still reads on white. |
| **Pinned-card / swipe-back component** (signal-card.state-pinned + btn-card-back.is-primary) | 834, 890, 903, 910, 917, 942, 974–978, 993, 1015 | A second mini-palette: amber `#c9963a` / `#b6852f`, maroon `#b23a48`, white-on-amber `#fff`, plus rgba(0,0,0, 0.03/0.05) hovers | Add `--pinned-bg`, `--pinned-border`, `--pinned-fg`, `--pinned-fg-strong`, `--card-back-fg`. Hovers (`rgba(0,0,0, …)`) become `--bg-hover-strong` (already loosely exists as `--bg-hover` line 10 but only for dark). |
| **Hardcoded text-on-accent** | 349 (`color: white` on `.btn`), 903 (`color: #fff` on btn-card-back) | Assumes accent is always dark enough for white text | `--text-on-accent`. Light-mode accent stays close enough to dark-mode accent that white-on-accent is still fine — but token it so we don't have to reason about it twice. |
| **Notice / inline-info tints** | 1237–1238, 1246–1248, 1261 | `rgba(0,122,204, …)` (info-blue, *not* `--info`), `rgba(192,57,43, …)` (a redder warn) | Either map to existing `--info-soft` / `--warn-soft`, or — if those hues are intentionally distinct — add `--notice-info`, `--notice-warn`. (Audit which.) |
| **Border tokens themselves** | 11–12 | `rgba(255,255,255, 0.07/0.13)` — *bakes in* a white-on-dark assumption | The tokens have to flip: in light mode borders are `rgba(0,0,0, 0.08/0.14)`. Same token name, different values per theme. |

Total work: tokenize the seven categories above. Most components don't change at all — they already reference the abstraction we're standardizing.

### Token rename pass (dark stays the default)

Done as a single mechanical rename PR *before* light mode. Old → new:

| Old | New | Notes |
| --- | --- | --- |
| `--bg` | `--surface-0` | Page background |
| `--bg-subtle` | `--surface-1` | Sidebar, panels, stat cards |
| `--bg-raised` | `--surface-2` | Hover, active nav, code blocks |
| `--bg-hover` | `--surface-hover` | The translucent overlay used on top of any surface |
| `--text` | `--text-primary` | |
| `--text-muted` | `--text-secondary` | |
| `--text-dim` | `--text-tertiary` | |
| `--border` | `--border-subtle` | |
| `--border-strong` | `--border-strong` | (kept) |

New tokens added in the same PR (still dark-only):

```
--text-on-accent: #ffffff;
--bg-overlay:        rgba(12,13,16,0.85);
--bg-overlay-strong: rgba(12,13,16,0.72);
--bg-glow:           rgba(94,106,210,0.18);   /* landing radial */
--accent-2:          #b8a8ff;                 /* gradient end */

--ok-soft:    rgba(76,183,130,0.08);
--ok-border:  rgba(76,183,130,0.30);
--warn-soft:  rgba(224,165,0,0.08);
--warn-border:rgba(224,165,0,0.30);
--err-soft:   rgba(239,68,68,0.10);
--err-border: rgba(239,68,68,0.30);
--info-soft:  rgba(96,165,250,0.08);
--info-border:rgba(96,165,250,0.30);

/* signal_type palette — full hoisted set, see audit table */
--type-funding:        #4cb782;
--type-funding-soft:   rgba(76,183,130,0.08);
--type-funding-border: rgba(76,183,130,0.35);
/* …new_hire, product_launch, integration, price_change, messaging_shift, momentum_point */

/* Pinned/swipe palette */
--pinned-bg:        rgba(201,150,58,0.06);
--pinned-bg-strong: rgba(224,165,0,0.04);
--pinned-border:    rgba(224,165,0,0.35);
--pinned-fg:        #c9963a;
--pinned-fg-strong: #b6852f;
--card-back-fg:     #b23a48;
```

The rename is 8 token names across the file. Cheap to do as a single replace-all per token. Visual diff at the end should be zero.

### Light palette

Drafted, not final — refine after seeing it in the browser. Contrast-checked against WCAG AA for body copy.

```
:root[data-theme="light"] {
  --surface-0:    #ffffff;
  --surface-1:    #f7f8fa;
  --surface-2:    #eef0f4;
  --surface-hover: rgba(0,0,0,0.04);

  --border-subtle: rgba(0,0,0,0.08);
  --border-strong: rgba(0,0,0,0.14);

  --text-primary:   #15171a;
  --text-secondary: #5a5e66;
  --text-tertiary:  #8a8d93;

  --accent:       #4854c7;   /* a hair darker than dark-mode #5e6ad2 — needed for AA on white */
  --accent-hover: #3b46b3;
  --accent-soft:  rgba(72,84,199,0.10);
  --accent-2:     #7a6cd6;   /* darker gradient end */

  --text-on-accent: #ffffff;

  --bg-overlay:        rgba(255,255,255,0.85);
  --bg-overlay-strong: rgba(255,255,255,0.72);
  --bg-glow:           rgba(94,106,210,0.10);

  /* status hues stay; tints get bumped on light to remain visible */
  --ok:   #2f9d6e;
  --warn: #b8861a;
  --err:  #d33a3a;
  --info: #2a6fd1;

  --ok-soft:    rgba(47,157,110,0.10);
  --ok-border:  rgba(47,157,110,0.30);
  --warn-soft:  rgba(184,134,26,0.10);
  --warn-border:rgba(184,134,26,0.30);
  --err-soft:   rgba(211,58,58,0.10);
  --err-border: rgba(211,58,58,0.30);
  --info-soft:  rgba(42,111,209,0.10);
  --info-border:rgba(42,111,209,0.30);

  /* type palette — same hues as dark, alphas bumped */
  --type-funding-soft:    rgba(76,183,130,0.14);
  --type-funding-border:  rgba(76,183,130,0.40);
  /* …others follow the same +0.06 alpha rule */

  /* pinned palette holds */
  --pinned-bg:        rgba(201,150,58,0.10);
  --pinned-bg-strong: rgba(224,165,0,0.08);
  --pinned-border:    rgba(224,165,0,0.40);
  --pinned-fg:        #a07a26;
  --pinned-fg-strong: #886423;
  --card-back-fg:     #9e2f3c;
}
```

### Toggle mechanism

- A `<button>` in the sidebar footer (next to the user avatar block, [base.html](app/templates/base.html)). Sun/moon icon, `aria-pressed` reflects state.
- Click handler is one inline `onclick`: toggle `data-theme` on `<html>`, write `theme=light|dark` cookie, no fetch.
- **Server reads the cookie at render time** and emits `<html data-theme="{{ theme }}">`. This avoids the flash-of-wrong-theme on first paint.
  - The cookie is read in a small dependency in [app/ui.py](app/ui.py) (or wherever the base context dict is built) and passed into the template context as `theme`. Default `dark`.
  - Cookie is plain, long-lived, no signing. It's a UI preference, not auth state.
- Anonymous users (landing page) get the same cookie path. No special-casing.

### Migration plan

Three PRs, in order. Each is shippable on its own — light mode is dark-and-light by the third.

1. **Token rename pass.** Replace the 8 token names listed above. No new tokens, no light theme. Visual diff = 0. Smoke-check: scroll through every page in dev. This is purely a clarity-of-naming improvement.
2. **Tokenize the literals.** Add the new tokens (`--ok-soft`, `--type-funding-soft`, `--pinned-*`, `--bg-overlay`, `--accent-2`, `--text-on-accent`). Replace each of the ~86 literals with the appropriate token. Still dark-only — `:root` block grows, no `[data-theme]` block yet. Visual diff = 0 at the end (tokens hold the same values the literals did).
3. **Add light theme + toggle.** Add the `:root[data-theme="light"]` block. Add the toggle button + cookie + server context. Smoke-check every page in both themes.

Each PR is mergeable independently. The first two harden the design system regardless of whether light mode ever ships.

### What "light mode" means concretely on each surface

A non-exhaustive list of the surfaces where the auditor needs to actually look (not just rely on tokens):

- **Stream cards** ([_stream_card.html](app/templates/_stream_card.html)) — the densest component, type chips and pinned states will both need light-tint verification.
- **Landing page** ([landing.html](app/templates/landing.html)) — the radial-gradient glow at [style.css:1729](app/static/style.css:1729) needs a much subtler tint on white, and the brand gradient end-stop needs to be deeper.
- **Chat bubbles** ([_chat_message.html](app/templates/_chat_message.html), styles at [style.css:2183-2250](app/static/style.css:2183)) — tool-confirmation and error tints need re-checking.
- **Charts** (the inline SVG in `_findings_volume.html`, `competitor_profile.html`) — segment fills today are hardcoded hex from [docs/dashboard/01-findings-volume-chart.md](docs/dashboard/01-findings-volume-chart.md). They'll need to ride `--type-*` tokens (already true for some). The chart axis text is `currentColor` — fine.
- **Code blocks / mono surfaces** — verify mono text contrast on `--surface-2`.

### Acceptance criteria

1. `<html data-theme="dark">` renders byte-for-byte identical to today (run a screenshot diff on `/stream`, `/dashboard`, `/competitors`, `/`, `/chat` after PRs 1+2). No regressions.
2. `<html data-theme="light">` renders the app readably across the same set of pages. WCAG AA on body text and primary buttons. Visual smoke on each.
3. The toggle in the sidebar persists across reload (cookie). No flash of wrong theme on first paint (server-side initial attribute).
4. `grep -E '#[0-9a-fA-F]{3,6}|rgba?\(' app/static/style.css` returns only matches inside the `:root` and `:root[data-theme="light"]` blocks. (Acceptable exceptions: `currentColor`, `transparent`, gradient stops that *are* tokens.)
5. Inline SVGs that today hardcode dark-friendly hex (chart segments, brand dots) pull from `--type-*` / `--accent-2` so they shift with the theme.
6. New tokens are documented under `## 01 — Light mode` in this file (this section). The next design pass appends `## 02 — …` rather than rewriting.

### Open questions

1. **Should we honor `prefers-color-scheme` when no cookie is set?** Probably yes, eventually — but post-v1. Do the manual toggle first, prove the theming works, then add the OS-pref fallback. One-line change to the cookie-read logic.
2. **Is the landing page in scope for v1?** Argument for: it's the first thing a signed-out visitor sees, branding-wise a light landing on a dark app is jarring. Argument against: marketing pages often *want* a fixed brand palette regardless of user pref. Default to "yes, theme it" — easier to revert than to add later.
3. **Pinned amber palette in light mode.** The amber on dark is rich and warm. On a white surface it tends toward muddy mustard. Worth one design pass to see if a slightly cooler "pinned" hue reads better in light mode — and if so, whether dark-mode pinned should match (probably not — separate values per theme is exactly what the tokens enable).

### What this unblocks

- High-contrast theme: a third `[data-theme="hc"]` block over the same token shape.
- Per-customer brand themes (post-multi-tenant): a fourth block, served by the cookie based on org settings.
- A `--motion-*` token family for transition durations (currently inline at 80ms in lots of places). Same shape, same approach. Would be `## 02 — Motion tokens` here.
