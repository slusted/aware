# Spec — Floating chat panel (slide-out from anywhere)

**Status:** Draft
**Owner:** Simon
**Depends on:** the existing chat surface (`docs/chat/01-chat.md`) — same `/api/chat/*` endpoints, same SSE handler, same DB rows.
**Unblocks:** asking the watch a question without leaving the page you're on. Chat becomes an always-available companion to Stream / Competitors / Market / Runs instead of a destination tab.

## Purpose

Right now `/chat` is a top-level nav item. Opening it means leaving whatever you were looking at — a stream entry, a competitor profile, a market digest — losing your scroll position and your mental context, asking the question, then navigating back. That's friction at exactly the moment chat is most useful: when looking at a row makes you want to ask about it.

This spec moves chat from "page you visit" to "panel you summon." A floating button bottom-right on every authenticated page; click it and a side drawer slides in from the right edge with the active conversation. Click again (or hit Esc) and it slides away, leaving the underlying page exactly as it was. The full `/chat` and `/chat/{id}` pages stay — same URLs, same templates — for users who want the maximised, two-pane experience. The drawer is the *quick* surface; the page is the *deep* surface.

## Non-goals

- **Removing `/chat` and `/chat/{id}`.** Those pages stay as-is. The drawer is additive — same data, second presentation. Power users who want the conversation list sidebar + main thread keep that view.
- **A new chat backend.** Zero changes to `app/routes/chat.py`, `app/chat/agent.py`, the SSE event shape, or the DB schema. The drawer is a frontend reorganisation that calls the same endpoints `chat.js` already calls.
- **Per-page contextual chat.** v1 does not auto-inject "you are looking at competitor Ashby" into the system prompt when the drawer is opened from `/competitors/ashby`. That's a tempting follow-up (see *Future*), but it changes the agent contract and deserves its own spec.
- **Multiple drawers / multi-conversation tabs in the drawer.** One drawer, one active session at a time. Switching sessions happens through the conversations list inside the drawer.
- **Mobile-specific layout work.** The drawer should not break mobile (it falls back to full-width sheet under ~640px), but optimising the mobile chat experience is out of scope.
- **Persisting drawer-open state across page loads.** Closing the drawer and navigating to a new page lands you with the drawer closed. No surprise re-open. (Open *while* navigating via HTMX would persist by accident — see *Edge cases*.)
- **Removing chat from the left sidebar.** It stays as a nav item linking to `/chat` (the deep view). Users who already learned to click "Chat" in the sidebar are not punished.

## Design principles

1. **Same data, second view.** The drawer renders the same `chat_session.html` content (thread, input, tool cards, cost meter) inside a `<aside>` that slides in. No fork of the rendering logic; the existing partials are reused.
2. **Summon, dismiss, resume.** The expected interaction is fast: click button → ask → close → keep working. Closing must not lose anything (in-flight stream finishes; partial input is preserved).
3. **The button is always there, never in the way.** Fixed bottom-right, ~56px circle, visible above every page's content but not over the status bar. Hidden on the auth pages (login/setup) where there is no `user`.
4. **No double-mount of `chat.js`.** The SSE handler is initialised once when the drawer mounts a session. Closing the drawer disconnects it cleanly so a re-open starts fresh.
5. **HTMX-first.** The drawer's content is fetched via `hx-get` from a new partial endpoint. That keeps the markup co-located with the existing Jinja templates and avoids a second JS-driven rendering path.
6. **The page underneath does not move.** The drawer overlays content; it does not push the main column. Scroll position is preserved when it opens and closes.
7. **Keyboard parity.** `Esc` closes. A configurable shortcut (`Ctrl+/` proposed) opens the drawer with focus on the input. `Cmd/Ctrl+Enter` to send still works inside the drawer (already wired in `chat.js`).
8. **Fail soft on missing key.** Same as `/chat` today — when `ANTHROPIC_API_KEY` isn't set, the drawer renders the "add a key" nudge inline instead of an empty thread.

## Where it lives

### New files

- `app/templates/_chat_drawer.html` — the drawer shell. Empty thread placeholder + a `hx-get` slot that loads the active session partial. Rendered once in `base.html` so it's available on every page.
- `app/templates/_chat_drawer_session.html` — the partial that the drawer's HTMX slot loads. Mirrors the body of `chat_session.html` (header meta, `#chat-thread`, `#chat-input-form`, notifications poller) but without `{% extends "base.html" %}` and without the page-level chrome (page title, "← All chats" link).
- `app/templates/_chat_drawer_picker.html` — the conversation list partial shown when no session is active (drawer just opened, or user clicked "Conversations" inside the drawer). Lists recent sessions + a "New chat" button. Same data shape as the `chat_index.html` sidebar.
- `app/static/chat-drawer.js` — the shell controller: open/close, keyboard shortcuts, swap-in/swap-out of the SSE handler, body scroll lock. Small (~150 LOC). Loads on every page; opting out by checking `document.querySelector('#chat-drawer')` exists.
- `docs/chat/03-floating-chat-panel.md` — this spec.

### Edits

- `app/templates/base.html`
  - Add `{% if user %}{% include "_chat_drawer.html" %}{% endif %}` after the `<main>` block so the drawer is in the DOM on every authenticated page.
  - Add `<script src="/static/chat-drawer.js?v=drawer-1"></script>` at the bottom of `<body>`. (Keep the existing `marked` + `chat.js` script tags inside the drawer partial so they only load when the drawer mounts a session — see *Asset loading* below.)
  - Add the floating launcher button: `<button id="chat-launcher" class="chat-launcher" aria-label="Open chat" aria-expanded="false">…</button>`. Same `{% if user %}` gate.
  - Optional sidebar tweak: leave the existing `Chat` nav link pointing at `/chat` (the deep view). No removal.
- `app/static/style.css`
  - New section under the existing `.chat-*` rules: `.chat-launcher`, `.chat-drawer`, `.chat-drawer-backdrop`, `.chat-drawer-open`, `.chat-drawer-header`. Slide transition via `transform: translateX(100%)` ↔ `translateX(0)`, `transition: transform 220ms cubic-bezier(.2,.7,.2,1)`. Width `min(420px, 100vw)`. `z-index` above the sidebar (which is `1`) but below modals if any (~`60`).
  - Drawer adopts the existing `.chat-thread`, `.chat-bubble-*`, `.chat-tool-card` rules — those already work inside any container.
- `app/static/chat.js`
  - **Currently** scopes itself to `document.querySelector('.chat-session')` and exits on `null`. Change scope to a passed-in root: export a small `initChat(rootEl)` factory and call it both from the page (`chat_session.html`) and from the drawer when a session loads. The IIFE wrapper stays; we just expose `window.AwareChat = { init, dispose }`.
  - Add a `dispose()` that aborts the in-flight `fetch` (via an `AbortController`) and detaches event listeners. Called when the drawer unmounts a session (close, switch, navigate).
  - The `?initial=` URL-param block stays gated on the page-mode root only.
- `app/routes/chat.py` — **add one route**: `GET /api/chat/drawer` returns one of:
  - the active session partial (`_chat_drawer_session.html`) for a given `?session_id=…`, or
  - the picker (`_chat_drawer_picker.html`) when called with no session id.
  Both are HTMX HTML responses, same auth/scope checks as the existing routes. No new SSE; the partial just emits the same DOM that `chat.js` already drives.
- `app/ui.py` — no changes needed. The drawer is a global include, not a page route.

### Why a server-rendered partial instead of pure JS

The full thread (with tool cards, results, markdown rendering, confirmation status) already has a Jinja partial path (`_chat_message.html` looped inside `chat_session.html`). Re-implementing that in JS would double-maintain rendering. The drawer issues `hx-get="/api/chat/drawer?session_id=…"`, drops the HTML in, and `chat.js` hydrates the markdown + wires the form. Same code path that already works for the page view.

## UX details

### The launcher button

- 56×56 circle, fixed `bottom: 24px; right: 24px`, accent-colored background, chat-bubble icon (reuse the `<svg>` from the sidebar `Chat` nav).
- Subtle shadow + 1px border so it works on light and dark themes.
- Hover: lifts 2px with a faster shadow; focus ring is the standard `--accent` outline used elsewhere.
- Hidden when:
  - The user is unauthenticated (no `user` in the template scope).
  - The drawer is open (the close affordance is inside the drawer header — having both a launcher *and* a close X visible at the same time was confusing in early sketch).
  - The user is on `/chat` or `/chat/{id}` already (offering a drawer over the page that *is* the drawer is silly).
- A small unread/active indicator dot when a background notification poll discovers new content (same data the existing `_chat_notification.html` poller surfaces). v1 dot is binary; counts can come later.

### The drawer

- Slides in from the right; backdrop is a dim layer (`rgba(0,0,0,0.35)` dark / `rgba(0,0,0,0.10)` light) that does not block clicks on the page below if `pointer-events: none` — clicking outside the drawer closes it via a backdrop click handler attached only to the backdrop element. **Decision needed:** do we let the user keep scrolling the page underneath while chat is open? Proposal: yes — pointer events pass through; the user can keep skimming Stream and ask a question about it. (Body scroll is *not* locked.)
- Header inside the drawer: conversation title + cost meter + rename + a "Conversations" button (toggles to the picker partial) + a close X. Same chrome as the page header, condensed.
- Body: the existing `#chat-thread` block.
- Footer: the existing `#chat-input-form` with `autofocus` on the textarea when the drawer opens to a session.
- Empty state (no active session): the picker — list of recent sessions + a "Start a new chat" textarea + example prompts. Submitting creates a new session via `POST /api/chat/sessions` (with `first_message`), then HTMX-swaps the drawer to the session partial. No full-page redirect.

### Active session selection

- First open of the drawer for a given browser session: load the most-recently-updated active session if one exists, otherwise show the picker. The chosen session id is stored in `localStorage` under `aware.chat.activeSessionId` so subsequent opens go straight back into the conversation.
- Switching sessions inside the drawer (via the picker) updates that key.
- Archiving / closing the active session in another tab is rare — if the partial 404s, the drawer drops back to the picker with a quiet "That conversation was archived" line.

### Asset loading

- `chat.js` and `marked` are heavy enough (~30KB combined) that loading them on every page just to power the drawer is wasteful for users who never open it. Strategy: the drawer's HTMX partial response includes the two `<script>` tags inline. They're loaded the first time the user actually opens chat, then cached. `chat-drawer.js` itself is small and global.
- (Alternative considered: lazy-`import()` from `chat-drawer.js`. Rejected because the existing `chat.js` is an IIFE, not an ES module, and converting it is out of scope for this spec.)

### Keyboard

- `Ctrl+/` (or `Cmd+/` on Mac) — toggle drawer. Focus moves to the input on open, returns to the previously-focused element on close.
- `Esc` — close drawer if open. Does not interrupt an in-flight stream; the SSE keeps running and the drawer animates out around it. Reopening shows the completed turn.
- `Cmd/Ctrl+Enter` — send (already wired by `chat.js`, works unchanged).
- `Tab` cycles through drawer focusable elements only while open (focus trap).

### Accessibility

- Drawer is `role="dialog"` with `aria-labelledby` pointing at the title.
- Backdrop click closes (primary), `Esc` closes (secondary), close button closes (tertiary).
- `aria-expanded` on the launcher reflects state.
- Drawer transition respects `prefers-reduced-motion`: replace the slide with an opacity fade.

## Edge cases & decisions

- **Streaming when the drawer closes mid-turn.** The fetch reader keeps running; events update DOM nodes still attached to the document (the drawer is hidden via transform, not removed). On reopen the user sees the completed thread. If the user navigates away while a stream is in flight, the browser cancels the request — same as today on `/chat/{id}`. No change.
- **HTMX page transitions.** This codebase uses regular full-page navigations (the only HTMX `hx-get` calls today are partial swaps, not boost-style page nav). The drawer is re-included on each new page render, so it returns closed by default. If we ever turn on `hx-boost`, we'd need to keep the drawer DOM stable across swaps — not blocking for v1.
- **Two tabs, same conversation.** Already works today on `/chat/{id}`. Drawer inherits the same property — last write wins on rename, cost is server-truthful.
- **In-flight rename / archive.** No interaction with the drawer; the existing endpoints handle it.
- **Status bar overlap.** The status bar (`#status-bar-slot`) sits at the bottom of `<main>`. The launcher button sits 24px above the viewport bottom — it overlaps the bar visually. Decision: launcher floats *above* the status bar with the same offset; the bar is short enough that this looks fine. If overlap turns out to bother me on dense status updates, raise the launcher to `bottom: 56px` and re-evaluate.
- **The Chat nav item is now redundant for most users.** True. Leaving it in (linking to `/chat`) is the cheap path — it's the deep view and we don't want to take it away. If telemetry shows it's unused after a few weeks, remove it then.

## Telemetry

- Console-log only for v1 (no analytics infra in the project today). Useful events for me when poking at the panel:
  - launcher clicked / drawer opened (and how — click vs shortcut)
  - drawer closed (and how — backdrop, Esc, X)
  - new session started from drawer
  - session switched from drawer
- These are diagnostic, not metrics. No need for a server endpoint.

## Migration & rollout

- Single PR. No DB migration. No new dependencies. No breaking change to `/api/chat/*` or `chat.js`'s public behavior.
- Feature flag *not* needed — the drawer is opt-in by clicking the launcher; users who never click it see no change.
- Manual test plan:
  1. Open any page (`/stream`, `/competitors`, `/market`). Launcher visible bottom-right.
  2. Click → drawer slides in. Empty state shows picker if no sessions; otherwise loads the latest.
  3. Type a question → SSE stream renders inside drawer. Tool cards appear. Confirmation buttons work.
  4. Close (X / Esc / backdrop). Drawer slides out; underlying page scroll preserved.
  5. Reopen on a different page. Drawer returns to the same session.
  6. Navigate to `/chat/{id}` directly. Same conversation, full-page view, drawer launcher hidden.
  7. Rename inside drawer → confirm rename appears in `/chat` sidebar after a refresh.
  8. Sign out → launcher gone.
  9. `Ctrl+/` toggles. `Esc` closes.
  10. Mobile (~360px wide): drawer goes full-width; launcher stays bottom-right.

## Future (not in this spec)

- **Page-aware system prompt.** When the drawer opens from `/competitors/ashby`, append a hidden context line `User is currently viewing competitor: Ashby (id 42)`. Cheap, but requires a contract decision — does the agent treat it as a hint or a hard scope? Spec separately.
- **Quick-prompt menu in the launcher.** Right-click the launcher for "Summarise this page" / "Run a deep research from here" — pre-baked prompts that include the current URL's primary entity.
- **Pinned messages / pin-to-page.** A way to anchor a chat answer to a specific stream entry so it shows up there next time. Bigger spec.
- **Notification badge with count.** Replace the binary dot with an unread count tied to scheduled-question replies.
- **Persist drawer-open across navigations.** Requires either `hx-boost` or a session cookie hint. Defer until I notice myself wanting it.
