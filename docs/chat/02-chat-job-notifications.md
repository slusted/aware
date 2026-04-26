# Spec — Chat job-completion notifications

**Status:** Draft
**Owner:** Simon
**Depends on:** Spec 01 (Chat). Reuses the existing `DeepResearchReport` lifecycle (`app/models.py:260`), the chat write-tool that creates them (`app/routes/chat/tools.py`, `run_deep_research`), and the `Run.triggered_by="chat:{session_id}"` audit string already specified in Spec 01 §Acceptance #12.
**Unblocks:** the user no longer has to poke the chat to find out a long-running job they kicked off finished. The chat surface stays in lockstep with the watch's real state.

## Purpose

Today, when a user types "kick off deep research on Employment Hero" in chat, the agent confirms, the job queues, and then the conversation goes silent for 5–20 minutes. To find out it's done, the user has to manually ask "is it ready yet?" and the agent calls `get_deep_research_report` to check. That round-trip is wasteful (the agent has to re-call a tool to learn what the DB already knows) and the user has to remember to poke.

This spec adds **passive awareness**: the chat page polls for terminal-state research reports that were triggered from this session, and when one flips to `ready`/`failed`, surfaces an inline notification bubble in the conversation with a link to `/competitors/{id}#research`. The notification is also persisted as a `ChatMessage` row so:

1. It's part of the scrollback — the user can come back tomorrow and still see "Research on Employment Hero finished at 14:32 — [view](...)".
2. The agent's next turn naturally sees it (Spec 01 builds the Anthropic `messages` list from DB rows). No extra plumbing — the agent just *knows* the job finished, no tool call needed.

The chip is a link to the report page, not a playback of the report. If the user wants the agent to summarise it, they ask in the next turn — the agent then calls `get_deep_research_report` deliberately.

## Non-goals

- **Real-time push (WebSockets, SSE subscriptions, server-push events).** A 15-second HTMX poll is precise enough for a 5–20-minute job and matches the pattern already used at `app/templates/_research_status.html:10` (`hx-trigger="every 10s"`). No new transport.
- **Browser notifications / native OS pings.** Out of scope. The user has to be on the chat page (or come back to it) to see the chip. v2 problem.
- **Notifying every kind of background job.** v1 scope is `DeepResearchReport` only — that's the long-pole job and the one the user explicitly asked about. `MarketSynthesisReport`, `Run` (scan, market_digest), and any future long-runner follow the same pattern but are added in later specs.
- **Cross-session notifications.** A research run kicked off in session A does not surface in session B, even for the same user. The mental model is "this conversation kicked it off, this conversation tells you when it's done." Sessions stay isolated.
- **A separate notifications inbox / bell icon.** No global tray. The notification lives in the conversation it belongs to. If the user wants a tray, that's a future spec and probably belongs at the dashboard level, not the chat level.
- **Re-rendering or summarising the report into chat.** The agent doesn't auto-pull the report body. The chip links to the existing report page; the user reads it there. If they want a summary, they ask in the next turn.
- **Notification dismissal / read-state tracking.** The chip is a persistent message in the thread, not an ephemeral toast. There's no "dismiss" button — it just sits in scrollback like any other message. v2 can add muting if it turns out to be noisy (it won't; one notification per multi-minute job).

## Design principles

1. **Persist as a `ChatMessage` row, not an ephemeral UI element.** The same row serves the UI (rendered as a notification bubble) and the agent's next-turn context (loaded by `build_anthropic_messages`). One source of truth.
2. **Discover via polling, idempotent at the DB level.** The poll endpoint runs a small SQL query — terminal-state research rows for this session that don't yet have a notification message. Inserting a notification is gated by an existence check; a duplicated poll never creates duplicate notifications.
3. **Reuse existing audit plumbing.** The `Run.triggered_by="chat:{session_id}"` convention from Spec 01 is the link between a `DeepResearchReport` and the chat session. No new FK, no new join table. If a run wasn't triggered by chat, no notification fires.
4. **Always-on poll, low cadence.** The chat page polls every 15 seconds while open. Server cost is one indexed lookup per poll per open chat tab; cheap. Avoid the smart-stop-polling logic of `_research_status.html` — chat is multi-job, harder to know when "all done" means.
5. **Notification rendering is its own role, not piggybacked on `tool_result`.** A new role value `"notification"` keeps the audit clean (filter `WHERE role='notification'` to see "what jobs surfaced to chat this week"). Anthropic-side, the agent's `messages` list converts notification rows into a synthetic `user`-prefixed line so the API accepts it (the API only knows `user`/`assistant`).
6. **Fail closed on "did the user actually trigger this?"** Only reports whose `Run.triggered_by` exactly matches `chat:{this_session_id}` notify. A report run from `/competitors/{id}` directly will never show up in chat — even if the same user is in both surfaces. This keeps the chat thread clean and the audit honest.

## Where it lives

- **Models**
  - `app/models.py` — `ChatMessage.role` adds `"notification"` to its valid value set (no schema migration; `role` is already `String(16)` and stores arbitrary tag strings). `tool_payload` on a notification row carries `{"kind": "deep_research_report", "id": <int>, "competitor_id": <int>, "competitor_name": str, "status": "ready"|"failed", "url": str}`.
- **Polling endpoint**
  - `app/routes/chat.py` — new route:
    - `GET /api/chat/{session_id}/notifications` → returns an HTML partial (HTMX-style; `Content-Type: text/html`). For each newly-completed report, inserts a `ChatMessage` row and renders one `_chat_notification.html` partial. If nothing is new, returns `204 No Content` so HTMX swaps nothing.
- **Notification creation helper**
  - `app/chat/notifications.py` — new module:
    - `discover_completed_research(db, session_id) -> list[ChatMessage]`. Pure function. Queries:
      ```sql
      SELECT dr.* FROM deep_research_reports dr
      JOIN runs r ON r.id = dr.run_id
      WHERE r.triggered_by = :chat_session_tag
        AND dr.status IN ('ready', 'failed')
        AND NOT EXISTS (
          SELECT 1 FROM chat_messages cm
          WHERE cm.session_id = :session_id
            AND cm.role = 'notification'
            AND json_extract(cm.tool_payload, '$.kind') = 'deep_research_report'
            AND json_extract(cm.tool_payload, '$.id') = dr.id
        )
      ```
      Inserts one `ChatMessage(role='notification', ...)` per matching row, commits, returns the new rows. Idempotent: a second call after the first commits returns `[]`.
- **Templates**
  - `app/templates/_chat_notification.html` — new partial. Renders one notification bubble: small system-styled card, an icon (`✓` for ready, `⚠` for failed), the human text ("Deep research on **Employment Hero** finished — [open report](...)"), the timestamp.
  - `app/templates/chat_session.html` — adds the polling div near the messages container:
    ```html
    <div id="chat-notifications-poller"
         hx-get="/api/chat/{{ session.id }}/notifications"
         hx-trigger="every 15s"
         hx-swap="beforeend"
         hx-target="#chat-messages"></div>
    ```
    Renders existing notification rows alongside other messages on initial page load (the role-dispatch in `_chat_message.html` picks the notification partial for `role='notification'` rows).
  - `app/templates/_chat_message.html` — role dispatch gains `{% elif role == 'notification' %}{% include "_chat_notification.html" %}{% endif %}`.
- **Agent context build**
  - `app/chat/agent.py::build_anthropic_messages` — when a `ChatMessage.role == 'notification'` is encountered, emit a synthetic `user`-role message with content like:
    ```
    [system notification, not from the user] Deep research report #{id} on {competitor_name} is now {status}. View at {url}.
    ```
    Place it in the message stream at its natural position (ordered by `ChatMessage.id`). The system prompt gets a one-line addendum: "Messages prefixed with `[system notification, not from the user]` are automated status pings, not user input. Acknowledge them naturally if the user asks; don't reply to them on your own."
- **Static**
  - `app/static/style.css` — `.chat-notification` selector for the bubble (different background, smaller text, link styled as the report URL).

## Data model

No schema migration. `ChatMessage.role` accepts a new tag value:

| Role | Created by | `content` | `tool_payload` |
|------|-----------|-----------|----------------|
| `notification` | `discover_completed_research()` | Empty (the rendered text comes from `tool_payload`) | `{"kind": "deep_research_report", "id": int, "competitor_id": int, "competitor_name": str, "status": "ready"\|"failed", "url": str, "finished_at": ISO-8601 str}` |

The pre-existing `chat_messages(session_id, id)` index is enough to keep the notification ordered correctly in the conversation. The poll-side existence check uses `chat_messages(role)` plus a JSON path on `tool_payload`; on SQLite this is a small filtered scan. If chat scale grows past a few thousand notifications per session, add a partial index later — out of scope for v1.

## Polling endpoint

`GET /api/chat/{session_id}/notifications`

- **Auth.** Requires the same auth as the rest of `/api/chat/*`: authenticated user, `session.user_id == current_user.id`. 404 (not 403) on mismatch — same convention as Spec 01.
- **Behaviour.** Calls `discover_completed_research(db, session_id)`. If empty, returns `204 No Content`. Otherwise, renders each new `ChatMessage` through `_chat_notification.html` and concatenates. Returns `200 OK` with `Content-Type: text/html`.
- **HTMX swap.** Caller uses `hx-swap="beforeend"` against `#chat-messages`, so each new notification appends as a new row at the bottom. Empty 204 response is a no-op for HTMX (per the spec: empty body = no swap).
- **Concurrency.** If two browser tabs are open on the same session and both poll simultaneously, the existence check race is benign — at worst we'd insert two notifications for the same report. Add a unique constraint at the DB layer? **No.** Cheaper: wrap the SELECT-then-INSERT in a single transaction with `BEGIN IMMEDIATE`; SQLite serialises writes anyway. The check + insert happen atomically per call. Acceptable for v1.
- **Polling cadence.** 15 seconds. Configurable via `CHAT_NOTIFICATION_POLL_S`; default 15. Lower bound 5s (don't hammer the DB).

## UI

### Notification bubble shape

```
┌──────────────────────────────────────────────────┐
│ ✓ Deep research on Employment Hero is ready.     │
│   Open report → /competitors/47#research         │
│   2026-04-27 14:32                               │
└──────────────────────────────────────────────────┘
```

- Distinct background (slightly different shade than user/assistant bubbles — neither side-aligned, full width with a left-edge accent stroke).
- Icon: `✓` for `ready`, `⚠` for `failed`. Failed bubble shows the report's `error` text inline if present (truncated to 200 chars).
- Link is the report URL `/competitors/{competitor_id}#research`, opens same tab.
- Timestamp is the report's `finished_at`, not the notification's `created_at` — the user cares when the *job* finished, not when the poll noticed.

### Where it appears in the thread

Rendered inline at its natural message position (by `ChatMessage.id`). When the poll inserts one mid-stream — i.e. the user is reading a previous turn while a research finishes — it appears at the bottom of the conversation, after whatever the most recent assistant turn is. If a streaming turn is in flight, the notification appears after the in-flight bubble. The user sees it appear without any disruption.

### Initial page render

`/chat/{id}` server-renders all `ChatMessage` rows in order, including any prior notifications. So a user returning the next morning sees their conversation ending with "Deep research on Employment Hero is ready — open report" right where they left it.

## Agent context handling

`build_anthropic_messages` renders a notification row as a synthetic `user`-role Anthropic message:

```python
# Example output for one notification row
{
    "role": "user",
    "content": (
        "[system notification, not from the user] Deep research report "
        "#13 on Employment Hero is now ready. View at "
        "/competitors/47#research."
    ),
}
```

System prompt addendum (one paragraph appended to `app/chat/system_prompt.md`):

> Messages that begin with `[system notification, not from the user]` are automated status pings about long-running jobs the user previously asked you to start. Don't reply to them on your own — the user can see them too. If the user references one ("did the research finish?", "what does the Employment Hero report say?"), use the URL or report id in the notification to call the right read tool (e.g. `get_deep_research_report`). Don't claim a job is still queued if a notification says it finished.

This is the whole agent-side change. No new tools, no new prompt branches — just a soft directive that the model handles the message correctly when it appears.

## Auth

- The polling endpoint is gated by the existing chat session ACL (Spec 01 §Auth). A user can only poll their own sessions.
- The notification only surfaces a report whose triggering run was tagged `chat:{session_id}` — so even if the chat ACL were lax (it isn't), a user can't see a report they didn't kick off.
- The link in the notification points to `/competitors/{id}#research`, which has its own ACL (any authenticated user can view competitors today). If competitor-level ACLs land later, the link will respect them — the notification just exposes the URL, doesn't bypass auth on the destination.

## Testing

- Open `/chat/{id}` with no chat-triggered research in flight — notifications poller runs every 15s, returns 204 each time, no DOM change.
- Kick off a deep research from chat. Confirm. Watch the chat over the next 5–20 minutes — when the underlying `DeepResearchReport.status` flips to `ready`, within ~15s a notification bubble appears at the end of the conversation. Click the link — opens `/competitors/{id}#research`, scrolled to the Research tab.
- Reload the page after the bubble appears — the notification re-renders on initial load (it's a persisted `ChatMessage` row).
- Kick off a deep research, force it to fail (e.g. invalid Gemini key) — the notification bubble appears with `⚠` styling and the error text. Acceptance: the failure is visible in chat, not just in the runs page.
- Send a message after the notification arrives ("what did it find?") — the agent's next turn includes the notification in `messages`, so it knows the report is ready. It calls `get_deep_research_report` on the right id (taken from the notification's `tool_payload`) and summarises. It does not say "still queued."
- Run a deep research from `/competitors/{id}` directly (no chat involvement) — no notification surfaces in any chat session. Acceptance: only chat-triggered runs notify chat.
- Open two browser tabs on the same chat session, wait for a research to finish — both tabs see one notification bubble (no duplicates). Acceptance: poll concurrency is safe.
- A user with two chat sessions, both having kicked off a research — each session's poll only surfaces its own session's report. Acceptance: cross-session isolation holds.
- Force the SQL query plan: with 1000 chat messages and 50 deep research reports across 10 sessions, the poll query stays under 50ms on SQLite. Acceptance: existence check is cheap.

## Acceptance criteria

1. Schema-free addition: `ChatMessage.role='notification'` rows insert without migration.
2. The poll endpoint exists, is auth-gated to the session owner, returns 204 when nothing is new, returns rendered HTML when something is.
3. Polling runs every 15s while the chat page is open. The HTMX trigger is wired in `chat_session.html`.
4. A `DeepResearchReport` whose triggering `Run.triggered_by` matches `chat:{session_id}` and whose status is `ready` or `failed` produces exactly one notification message, regardless of how many polls happen.
5. A `DeepResearchReport` whose triggering run is NOT chat-tagged produces zero notifications in any chat session.
6. The notification is persisted as a `ChatMessage` row and re-renders on page reload at the same position in the thread.
7. The notification is rendered as a distinct bubble (not user, not assistant) with status icon, competitor name, link to the report page, and the report's finished-at timestamp.
8. Failed-status notifications include the `DeepResearchReport.error` text (truncated to 200 chars) inline.
9. The agent's next turn after a notification arrives includes the notification in its Anthropic `messages` list as a synthetic `user`-role message with the documented prefix. The agent does not respond to it on its own initiative; it only references it when the user asks.
10. The system prompt addendum is added to `app/chat/system_prompt.md` and re-registered in `KNOWN_SKILLS` (no new skill needed; same `chat_system` skill).
11. CSS cache-buster bumped; `.chat-notification` styles are isolated and don't affect other surfaces.
12. Two browser tabs polling the same session never produce duplicate notifications.

## What this unblocks

- **Same pattern for other long-runs.** `MarketSynthesisReport`, scheduled `Run` rows kicked off from chat (`run_market_digest`, `run_market_synthesis`) all gain the same notification surface by adding their kind to `discover_completed_research` (or extracting it to a small dispatch). One row per kind in the helper.
- **Cross-session "your other chat just got a result" tray.** Once notifications are persisted, a tray that aggregates "any of my chats has a fresh notification" is a single SQL query. Future spec.
- **Email / Slack ping when chat is closed.** The notification row is the persistence anchor. A small post-insert hook can fan out to email/Slack if the user has been away from the chat page for >X minutes. v2.
- **Auto-summarise on arrival.** A future flag could make the agent automatically call `get_deep_research_report` and post a one-paragraph summary alongside the notification. Skipped in v1 because the user explicitly said "doesn't need to play back whole report" — the link is enough. Easy to add later if usage shows people always ask for the summary.
