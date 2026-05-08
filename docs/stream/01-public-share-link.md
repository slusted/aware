# Spec — Public share link for a saved filter

**Status:** Draft
**Owner:** Simon
**Depends on:** existing `SavedFilter` model (`app/models.py`), filter CRUD routes (`app/routes/filters.py`), the stream surface (`app/ui.py::stream_page`, `_parse_stream_filters`, `_stream_query`, `_stream_card.html`).
**Unblocks:** sharing a curated slice of the watch with someone who doesn't have an account — a partner, an investor, a teammate at another company — without handing over a login or exposing the full feed.

## Purpose

Saved filters already let a user pin a slice of the stream they care about ("Acme + Beanie, product signals only, last 30 days"). Today that slice is locked behind auth: the only way to show it to anyone outside the team is a screenshot or a copy-paste.

This spec adds a third visibility on `SavedFilter`: **public via unguessable link**. The owner clicks Share on a saved filter, gets a `https://aware.app/p/{token}` URL, and anyone with that URL sees a read-only render of the same stream — no login, no swipe, no internal-only fields. Rotating or revoking the token kills the link without affecting the filter itself.

The unit of sharing is the saved filter, not "my whole feed." That keeps the blast radius scoped to whatever the owner deliberately curated, and makes "stop sharing this slice" a single action against a single row.

## Non-goals

- **Sharing the full unfiltered stream.** A whole-feed token is a bigger commitment (every signal we ever surface) and a different UX. If you want to share everything, save a filter with no constraints and share that.
- **Per-link audiences / per-link expiry / per-link analytics.** v1 is one token per filter, stored on the `saved_filters` row. A separate `filter_shares` table with multiple links, expiries, view counters, and access logs is a clean follow-up — see *Future*. Picking the simpler shape now means we don't lock the schema before we know what we actually want.
- **Allowing a public viewer to interact.** No pin / dismiss / swipe / feedback / "include dismissed" toggle / debug `?explain=`. Read-only render only. Anything that writes to `user_signal_events`, `signal_views`, or the ranker is hidden because there is no user.
- **Editing the filter from the public page.** No filter-bar UI on the public render. The viewer sees what the owner curated; if they want to change it, they're not the audience.
- **Password-protected links / one-time-view links / email-gated links.** All three are reasonable v2s; v1 is "anyone with the URL." Documented under *Future* so we don't bake assumptions that block them.
- **Embedding via iframe / oEmbed.** Could come later. For v1 the public page is just a normal HTML page; if a user wants to iframe it that works incidentally, but we don't ship a JSON oEmbed endpoint or a CSP friendly to embedders.
- **A public version of the *team* filter "default" surface.** `User.default_filter_id` is a per-user choice; it doesn't transfer. Sharing applies to a specific filter row, not to "whatever filter user X currently defaults to."

## Design principles

1. **One token per filter, on the row.** Add `public_token` (nullable, unique) to `saved_filters`. No new table. Mint = set; rotate = overwrite; revoke = null. If we later want multiple-links-per-filter we migrate to `filter_shares` then; today's UI doesn't ask for it.
2. **Token is opaque, ~32-char urlsafe.** `secrets.token_urlsafe(32)` (already used in `app/auth.py:64`). 256 bits of entropy — uncrawlable, can't be guessed, safe to put in URLs and logs.
3. **Public route is its own surface, not a "stream with `auth=False`."** The existing `stream_page` is heavily user-scoped (joins `SignalView` for state, queries `UserSignalEvent` for view counts, supports `pinned_only`, drives ranker feedback). A public render that pretends to be a user would be brittle; a separate route that calls a smaller helper keeps the surfaces honest.
4. **`_stream_card.html` is the same template, with public-render flags.** Same approach as `docs/competitor-profile/01-findings-as-cards.md`: the card is parametric — pass `public=True` and the template hides the new-dot, view-count chip, swipe affordances, dismiss/pin actions, and the debug `?explain` chips. Owner-side stream stays unchanged.
5. **The viewer sees what the filter says, today.** No snapshot. If the owner edits the filter spec, the public page's contents move. This is the feature: "share my Acme view" should keep working when I tweak the threshold. If we later want a frozen snapshot variant ("share *these specific* findings as of today"), it's a different mode — see *Future*.
6. **Permission to mint mirrors permission to delete.** Owners mint for their own private filters. Admins mint for team filters (`owner_id IS NULL`). A teammate can't quietly publish a team filter the rest of the team relies on; same gate `app/routes/filters.py::delete_filter` already uses.
7. **Hard-disable spec keys that don't make sense without a user.** A filter with `pinned_only=True` cannot be shared (it'd render an empty stream — pins are per-user). API rejects the mint with a 400 explaining why, UI hides the Share button on pinned-only filters.
8. **Don't index, don't leak.** Public page sends `X-Robots-Tag: noindex, nofollow` and a `<meta name="robots" content="noindex">`. Token never appears in server-rendered HTML beyond the page URL itself, never in analytics events, never in error pages.
9. **No new dependencies, no new infrastructure.** Same FastAPI + Jinja + HTMX + SQLite stack. One Alembic migration; one new route; one template; minor edits to the saved-filter UI.

## Where it lives

### Models + migration

- `app/models.py::SavedFilter` — add two columns:
  - `public_token: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True, index=True)`
  - `public_token_created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)`
- `alembic/versions/<new>_saved_filter_public_token.py` — adds both columns, creates a unique index on `public_token`. Backfill is null for all existing rows.

### Routes

- `app/routes/filters.py` — three new endpoints, same auth model as the rest of the file:
  - `POST /api/filters/{filter_id}/share` → mint or rotate. Returns `{public_token, share_url}`. Owner-or-admin check; rejects `pinned_only=True` specs with 400.
  - `DELETE /api/filters/{filter_id}/share` → null the token; same permission check. Returns `{ok: true}`.
  - `GET /api/filters/{filter_id}/share` → returns `{public_token, share_url}` or `{public_token: null}`. Used to render the Share button's current state without fetching the whole filter list.
- `app/routes/public.py` — **new file**, single-purpose, no auth dependency:
  - `GET /p/{token}` → look up `SavedFilter` by `public_token` (404 if not found / null). Parse `spec` via `_parse_stream_filters` (same helper, same defaults). Call a new `_public_stream_query(db, filters)` that mirrors `_stream_query` minus all `user`-scoped joins. Render `public_stream.html` with `noindex` headers.
- `app/main.py` — register the new router. No auth middleware on the `/p/*` prefix.

### Stream query

- `app/ui.py::_public_stream_query(db, filters)` — new helper next to `_stream_query`. Differences:
  - No `outerjoin(SignalView, …)`; no `pinned_only` branch (rejected at mint time anyway); no `include_dismissed` branch (always exclude dismissed isn't possible without a user — instead: ignore the flag and return the unfiltered set, since "dismissed by user X" is meaningless to a public viewer).
  - No `view_counts` query.
  - `tagged="untagged"` is honoured (it's user-agnostic — predicate evidence is per-finding, not per-user).
  - Same materiality / type / competitor / window / since_days / downweight_stale logic.
  - Returns `(findings, has_more)` only — no `views`, no `view_counts`.

### Templates

- `app/templates/public_stream.html` — **new**, minimal `<html>` shell (no `base.html`; we don't want the sidebar, top nav, chat launcher, or the user menu). Renders:
  - A small header: filter name, "Shared from Aware", a low-key Aware wordmark linking to the marketing site.
  - The stream list, looping `_stream_card.html` with `public=True, show_competitor=True, expandable=False`.
  - Footer: "Snapshot reflects live data — this view updates as new signals come in" and a `<small>` line with "Last updated {{ now }}".
- `app/templates/_stream_card.html` — gain a `public` flag (default `False`). When `True`, hide:
  - The `.new-dot` (per-user state).
  - The view-count chip.
  - The swipe / pin / dismiss / snooze actions inside the card footer (whatever they currently are — same elements the competitor-profile spec already strips with `show_rating=False`).
  - The `?explain=` debug chips (`semantic-chip`, scorer breakdowns).
  - The state-`*` class on the article (always render as `state-new` or, better, drop the class entirely so there's no "unread" styling).
- `app/templates/saved_filters_*.html` (whichever partial renders the saved-filter list / dropdown) — add a Share button per filter row. States: "Share" (no token), copyable URL + "Rotate" + "Stop sharing" (token present), hidden entirely (filter is `pinned_only`). Use HTMX to call the three new `/api/filters/{id}/share` endpoints; no full-page reloads.

### CSS

- `app/static/style.css` — new section near the existing stream rules: `.public-stream-page` (page wrapper), `.public-stream-header`, `.public-stream-footer`. Reuse `.stream-list`, `.signal-card*`, all the badge / materiality / cluster-chip rules unchanged. Bump the `?v=` cache-bust on `style.css` per the project convention.

## UX details

### Owner side

- Each saved filter in the dropdown / management list grows a Share affordance. Closed state: a `Share` link.
- Click → small inline panel (HTMX swap, no modal) with:
  - The full URL in a read-only input + a Copy button (uses the Clipboard API; falls back to selecting the text).
  - "Anyone with this link can view this filter. They can't change it."
  - A Rotate link (mints a new token, invalidates the old URL).
  - A Stop sharing link (nulls the token).
- Pinned-only filters render the row without the Share affordance and a `<small>` tooltip explanation: "Pinned-only views are personal — share a different filter."
- Team filters: only admins see the Share affordance. Non-admins see the filter row without it (no error, no nag).

### Public viewer side

- Lands on `/p/{token}`. Page renders in ~the same width as the authed stream so the cards look identical to what the owner sees.
- No login link, no signup CTA in v1. (A small "Built with Aware" footer link is fine if it points at the marketing site, not at `/login`.)
- If the token is rotated or revoked: 404 with a generic "This share link is no longer active" page. We do not distinguish between "never existed" and "revoked" — that's the right confidentiality default.
- If the filter row is deleted entirely: same 404. Filter delete should null the token first as a matter of housekeeping; but the lookup is `WHERE public_token = ? AND public_token IS NOT NULL`, so a stale token from a deleted row simply doesn't resolve.
- Empty result set: render the header + a polite "No signals match this view right now." Don't expose internals like the spec JSON.

### What the public card hides

The public render of `_stream_card.html` strips:
- New-unread dot (`.new-dot`).
- The "Viewed N×" chip (per-user count).
- Pin / dismiss / snooze / rate buttons.
- `state-pinned` / `state-dismissed` / `state-snoozed` classes (no user, no state).
- The semantic / explain debug chips (these are always behind `?explain=1` for owners; we just never set it on the public render).

What it keeps:
- Competitor name + logo, signal type, materiality bar, source label, published date, title, scraped-content expand (off by default), cluster chip, finding URL link-out.

## Edge cases & decisions

- **Owner edits the filter spec.** Public URL keeps working; contents shift accordingly. Documented in the share panel: "Anyone with this link sees this filter as it is right now."
- **Owner deletes the filter.** Cascade-null the token first (defensive — even though the row is gone, ensure no orphan token can collide). 404 on the public URL.
- **Owner converts private → team or team → private.** Token survives the visibility flip — it's its own state. Team-to-private with an existing token is fine; the previously-shared link still works for the people who already had it. (If we want "rotate token on visibility change," that's an explicit toggle, not implicit.)
- **Two filters end up with the same token by collision.** Won't happen with 256-bit tokens, but the unique index makes it a hard error rather than a silent overwrite. Mint retries on `IntegrityError` (one retry, then 500 with a log).
- **Token in server logs.** `/p/{token}` will appear in access logs. Fine for now (single-tenant prod, logs are private). If we ever ship multi-tenant, scrub the path in the access log middleware.
- **Search engines and link unfurlers.** `noindex, nofollow` plus a deliberately bland `<title>` ("Aware — shared signals view") and no Open Graph image. Slack / iMessage will unfurl with the title alone — acceptable. If a user actively wants to share to a public channel and not be unfurled, they should be aware that link previewers may still cache the title; document this in the share panel as a one-liner.
- **Rate-limiting public reads.** v1 has no rate limit. The query is the same one we already serve on `/stream` and findings cache reasonably well in SQLite. If it becomes an abuse vector, add a per-token bucket later.
- **Materiality / scoring debug fields on findings.** Anything in the model that's marked "internal" should already be hidden by `_stream_card.html` for non-debug viewers. The public card is implicitly never in debug mode (don't honour `?explain=` on `/p/*` — drop it from the parsed filters).
- **Long-running stale cards.** If `downweight_stale=True` (default), 1+ year-old findings still appear at the bottom. That's the same behavior the owner sees. No special filtering.

## Telemetry

- Console-log only for v1 (consistent with the rest of the project — no analytics infra). Useful events on the server side:
  - `share_token_minted` — `{filter_id, owner_id, rotated: bool}`
  - `share_token_revoked` — `{filter_id, owner_id}`
  - `public_view_served` — `{filter_id, has_results: bool}` (no IP, no UA, no token in the log line)
- A view counter on the row is tempting but invites scope creep; defer to the `filter_shares` table when we build it.

## Migration & rollout

- Single PR. One Alembic migration (two columns + unique index). One new route file, one new template, one new CSS section, edits to `_stream_card.html` and the saved-filter UI.
- No feature flag — the feature is opt-in at the filter level (you have to click Share).
- No backfill. Existing filters have `public_token IS NULL` and behave exactly as before.
- Manual test plan:
  1. Create a private saved filter with competitor + signal-type constraints. Click Share. Confirm a `/p/{token}` URL appears.
  2. Open the URL in a private window (logged out). Confirm the same findings render. Confirm: no nav, no chat launcher, no swipe, no pin buttons, no view counts, no new-dots, no `?explain=` chips.
  3. Edit the filter on the owner side (raise materiality threshold). Reload the public URL. Confirm the cards filter accordingly.
  4. Click Rotate. Confirm the old URL 404s and the new one works.
  5. Click Stop sharing. Confirm the URL 404s.
  6. Try to share a `pinned_only=True` filter. Confirm Share is hidden and the API returns 400 if called directly.
  7. As a non-admin, try to share a team filter (`owner_id IS NULL`). Confirm Share is hidden and the API returns 403.
  8. Delete the filter row entirely. Confirm the URL 404s.
  9. Hit a random `/p/abcdefg` token that doesn't exist. Confirm 404 with the generic message (does not distinguish "never existed" vs "revoked").
  10. View-source on `/p/{token}`. Confirm `noindex, nofollow` is present.

## Future (not in this spec)

- **`filter_shares` table.** Multiple links per filter, each with its own optional expiry, optional password, optional label ("for the Acme call"), and a viewer-count column. Migrate `public_token` onto a row in this table when we want any of these.
- **Snapshot mode.** A second link variant that freezes the rendered findings at mint time, so the recipient sees "the stream as of last Tuesday" even if the filter or the underlying findings change. Useful for status reports and post-mortems; complicates the data model (need to capture finding ids, not re-query).
- **Password-gated links / email-gated links.** Trivial extension of `filter_shares`. Worth waiting until a user actually asks.
- **A "Subscribe to this view" affordance.** Public viewer drops their email and gets a weekly digest of new findings against the same spec. Opens up a different content moderation question (we'd be sending email on the owner's behalf).
- **Aware account upsell on the public page.** A "Watch your own competitors" CTA for unauthed viewers. Marketing decision; out of scope for the engineering spec.
- **oEmbed / iframe-friendly variant.** A `/p/{token}/embed` mode with a stripped chrome and CSP that allows `frame-ancestors *` for users who want to drop the view into Notion / a wiki.
- **Analytics on shared views.** Per-token view counts, referrer breakdown, last-viewed-at. Belongs with `filter_shares`, not on the row.
