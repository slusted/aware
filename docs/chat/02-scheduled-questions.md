# Spec — Scheduled chat questions (recurring agent runs, emailed)

**Status:** Draft
**Owner:** Simon
**Depends on:** Chat spec 01 (`app/chat/agent.py`, `ChatSession`, `ChatMessage`), the existing `app/scheduler.py` (APScheduler in-process), and `mailer.py::send_email` (Gmail SMTP, already wired).
**Unblocks:** Standing questions you want answered on a cadence — "what's new with our top 5 competitors this week?", "any pricing moves in the last 7 days?" — without needing to remember to open the app and ask.

## Purpose

The chat agent is great at one-shot investigation, but the highest-value questions are the ones you'd ask *every week* if you remembered to. Today there's no way to say "run this prompt every Monday at 8am and email me the answer." That's the gap.

A **schedule** is one row: a saved prompt + a cron + an email recipient. When the cron fires, the scheduler creates a fresh `ChatSession` for the prompt's owner, runs one headless turn through the existing agent, and emails the final assistant text. The session is preserved in `/chat` so the user can open it, see what tools the agent called, and ask follow-ups in the same thread.

Critical primitive: a `{{since}}` placeholder in the prompt, substituted with the previous run's timestamp. The agent then naturally filters its `search_findings` calls to "since last run" — so digests describe *new findings*, not the same five every week.

## Non-goals

- **A general workflow engine.** One prompt → one turn → one email send. No DAGs, no chained schedules.
- **Per-recipient filtering of the answer.** Every recipient gets the same email body. The existing digest's per-team-member competitor filter (`mailer.py::send_digest_to_team`) is a future addition once we know what filtering rules people want.
- **Inline templating beyond `{{since}}` and `{{now}}`.** No conditionals, no loops, no per-recipient variables. Add deliberately if a real need shows up.
- **Streaming the run.** Scheduled runs are headless — the agent runs to completion server-side and the email goes out when it's done. No SSE, no progress bar.
- **Replies from unrecognised senders.** A reply only becomes a follow-up turn if the sender's email matches a row in `users`. Anything else is logged and ignored — no spoof-by-Reply-All into the agent.
- **Group threading.** When a recognised recipient replies, their reply forks a *new, personal* session — it does not append to a shared "team thread" the other recipients can see. Multi-user conversations in one session are a future spec.
- **External cron.** No cron daemon, no GitHub Actions, no Cloud Scheduler. Same in-process APScheduler that runs the daily scan.
- **Per-schedule model selection.** Inherits the chat default model (currently `claude-sonnet-4-6`). Add a per-schedule override only if asks pile up.

## Design principles

1. **Reuse the chat agent.** A scheduled run is a chat turn. Same tool catalog, same cost ledger, same audit trail. No parallel "report builder" pipeline.
2. **One run = one ChatSession sent to N recipients.** The agent runs once, produces one answer, and the email goes out to every recipient with the same body and the same link. The owner can open the session in `/chat/{id}` to see what the agent looked at.
3. **Reply-to-converse via fork.** When a recognised recipient (sender email matches `users.email`) replies to a scheduled-question email, the system **forks** the original session — creates a new `ChatSession` owned by that user, copies the prior turns as context, appends the reply as a new user turn, runs the agent, and emails the answer back. Subsequent replies in the same email thread continue in that forked session. This keeps each user's follow-ups private, their cost ledger correct, and the role-based tool catalog applied to *their* role (not the schedule owner's).
4. **`{{since}}` is the delta primitive.** Substituted with the previous successful run's `created_at` (ISO 8601). On the first run, defaults to "7 days ago" so the first email isn't empty.
5. **Email body is plain text.** Assistant's final message verbatim, a footer with the chat link and an opaque ref token (used to route replies), and the email subject prefixed `[Watch Question]` so the IMAP poller can filter cleanly. No HTML templating in v1 — keeps `mailer.py` unchanged.
6. **Failures email too.** If the agent errors or the turn times out, every recipient gets an email saying "Scheduled question X failed: <reason>" with a link to the (now error-tagged) session. Silent failure is the worst outcome for a recurring job.
7. **Schedules are user-owned.** Each row belongs to one user; the *scheduled* run executes as that user. Forked reply-runs execute as the replying user. No service account.
8. **Single-replica, same caveat as the existing scheduler.** Documented at the top of `app/scheduler.py`. If we ever scale out, the swap is to a jobstore with locking — not a redesign.
9. **Cron expressions, not "every N days".** Power users want "8am Monday"; "every 7 days starting from when I created it" drifts. Use APScheduler's `CronTrigger` directly. UI offers a few presets (daily 8am, weekly Mon 8am, monthly 1st 8am) plus a raw cron field.

## Where it lives

- **Models + migration**
  - `app/models.py` — new `ChatSchedule` class.
  - `alembic/versions/<new>_chat_schedules.py` — create the table with indexes.
- **Scheduler integration**
  - `app/scheduler.py` — on `start()`, after the existing jobs, call `_register_chat_schedules(sched)` which loads enabled `ChatSchedule` rows and adds one APScheduler job per row (id = `chat_schedule_{schedule_id}`). Re-registration helpers `register_schedule(schedule_id)` / `unregister_schedule(schedule_id)` called from CRUD routes. **Also** registers a single recurring `chat_reply_poll` job (interval = `CHAT_REPLY_POLL_MINUTES`, default 5) that calls `app.chat.replies.poll_replies()`.
  - `_run_chat_schedule(schedule_id: int)` — the cron entrypoint for a scheduled question. Loads the row, runs `app.chat.scheduled.run_scheduled_question(schedule_id)`.
- **Headless turn runner**
  - `app/chat/scheduled.py` — new module:
    - `run_scheduled_question(schedule_id)` — load schedule, substitute placeholders in the prompt, create a fresh `ChatSession` owned by the schedule's user, drain `agent.run_turn()` to collect the final assistant text, fan out the email to all recipients, update `last_run_at` / `last_session_id` / `last_status`.
    - `_substitute(prompt, schedule) -> str` — replaces `{{since}}` and `{{now}}`.
    - `_collect_final_text(session_id, user_text) -> tuple[str, str]` — returns `(assistant_text, status)` where status is `"ok"` / `"error"` / `"timeout"`. Internally consumes the SSE-shaped events from `run_turn` and concatenates `text_delta`s belonging to the final assistant message (i.e. the one whose `stop_reason == "end_turn"`).
    - `_fanout_email(recipients, subject, body)` — iterates and calls `mailer.send_email` per recipient. Returns a `{email: ok|error}` map written to the schedule row's `last_recipient_status` JSON column for visibility.
- **Reply poller**
  - `app/chat/replies.py` — new module:
    - `poll_replies()` — IMAP scan for unseen messages whose subject starts with `[Watch Question]` and whose subject begins with `Re:` (mirrors the existing `mailer.check_for_replies` shape; do not reuse that function — it filters by `config["team"]` and a different subject prefix).
    - `_route_reply(msg) -> RouteResult` — extract the ref token (`[ref:s{session_id}]`) from the body or quoted body; look up the session; verify the sender's address matches a `users.email` (case-insensitive); fork or continue per the rules below.
    - `_fork_session(original_session, replying_user) -> ChatSession` — creates a new session owned by the replier, copies the original session's prior `chat_messages` rows verbatim (new ids, new `session_id`), sets `forked_from_id` on the new session.
    - `_append_reply_and_run(session, reply_text)` — adds a `user` message and runs `agent.run_turn`, then emails the assistant's final text back to the replier (subject: `Re: [Watch Question] ...`, same ref token format keyed to the forked session id so further replies route correctly).
- **Routes**
  - `app/routes/chat.py` — add:
    - `GET /chat/schedules` (UI) — list of the caller's schedules.
    - `GET /chat/schedules/new` and `GET /chat/schedules/{id}/edit` (UI) — form.
    - `POST /api/chat/schedules` — create.
    - `POST /api/chat/schedules/{id}` — update.
    - `POST /api/chat/schedules/{id}/run` — fire immediately (out-of-band, doesn't shift the cron). Useful for "test my prompt before committing to the cadence".
    - `POST /api/chat/schedules/{id}/toggle` — enable/disable.
    - `DELETE /api/chat/schedules/{id}` — hard delete (no soft-archive — schedules are config, not history; the per-run `ChatSession` rows hold the history).
- **Templates**
  - `app/templates/chat_schedules_index.html` — list view.
  - `app/templates/chat_schedule_form.html` — new/edit form.
  - The existing `chat_session.html` shows scheduled-run sessions unchanged; a small "from schedule: <name>" badge in the header when `session.scheduled_id` is set.
- **Nav**
  - In `app/templates/base.html` and the `/chat` index header, add a "Schedules" link next to "+ New chat". No new top-level nav item.

## Data model

```python
class ChatSchedule(Base):
    """A recurring chat question.

    One schedule = one prompt + one cron + one recipient. Owned by a
    user; runs execute as that user (same role, same tool catalog).
    Hard-deleted on user request — the per-run ChatSession rows it
    spawned are kept and remain accessible from /chat.
    """
    __tablename__ = "chat_schedules"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )

    # Short human label shown in the list and the email subject.
    title: Mapped[str] = mapped_column(String(255))

    # The prompt text sent as the first (and typically only) user
    # message. Supports {{since}} and {{now}} placeholders.
    prompt: Mapped[str] = mapped_column(Text)

    # APScheduler cron expression in five-field form:
    # "minute hour day_of_month month day_of_week"
    # e.g. "0 8 * * mon" = Monday 8am.
    cron: Mapped[str] = mapped_column(String(64))

    # Recipient email addresses. Stored as a JSON array on the row
    # rather than a join table — the list is short (typically <10),
    # always edited as a unit from the form, and never queried by
    # individual address (reply auth checks against `users`, not this
    # column). Defaults to [owner.email] at creation time.
    #
    # Validation: each entry must look like an email; max 25 entries
    # per schedule (CHAT_SCHEDULE_MAX_RECIPIENTS).
    recipient_emails: Mapped[list[str]] = mapped_column(JSON, default=list)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    # Updated after each run (success or failure). Used to substitute
    # {{since}} on the next run. NULL on a brand-new schedule.
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # The ChatSession id from the most recent run (success or failure).
    last_session_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="SET NULL"), nullable=True
    )
    # Overall run status. "ok" | "error" | "timeout" | "partial_email"
    # (some recipients failed) | "no_email" (mailer unconfigured).
    # NULL until the first run.
    last_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Short error blurb when last_status != "ok". Capped at 500 chars.
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # {"alice@x.com": "ok", "bob@x.com": "error: smtp 550", ...}
    # Populated on the most recent run. Powers the per-recipient
    # status badges in the UI.
    last_recipient_status: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
```

**Plus** two columns added to `chat_sessions`:

```python
# Set when a session was kicked off by a schedule. NULL for normal
# user-initiated sessions. Used to render the "from schedule" badge
# and to surface the run history under each schedule.
scheduled_id: Mapped[int | None] = mapped_column(
    ForeignKey("chat_schedules.id", ondelete="SET NULL"),
    nullable=True, index=True,
)

# Set when a session was forked from another (currently only by the
# reply-to-converse flow — a recognised recipient replies to a
# scheduled-question email and we copy the original session's
# messages into a fresh session owned by the replier). NULL for
# normal sessions and for the original scheduled-run session itself.
forked_from_id: Mapped[int | None] = mapped_column(
    ForeignKey("chat_sessions.id", ondelete="SET NULL"),
    nullable=True, index=True,
)
```

**Indexes:**
- `chat_schedules(user_id, enabled)` — sidebar list + scheduler boot scan.
- `chat_sessions(scheduled_id, created_at DESC)` — per-schedule run history.
- `chat_sessions(forked_from_id)` — "show all forks of this session" query for the schedule owner's view.

## Placeholder substitution

Two placeholders, both optional:

| Placeholder | Substituted with |
|-------------|------------------|
| `{{since}}` | `last_run_at` formatted as ISO 8601 (`2026-04-20T08:00:00Z`). On the first run (`last_run_at IS NULL`), substitutes the timestamp **7 days ago** so the first email has content. |
| `{{now}}`   | Current UTC timestamp at run time, ISO 8601. |

Substitution is plain string replace, no escaping. The model sees the substituted text directly. Example prompt:

> Summarise what's new with our top 5 competitors since `{{since}}`. Lead with anything material (pricing, leadership, funding, ATS migrations). Cite finding ids. Skip categories with no new activity.

## Run flow

`app.chat.scheduled.run_scheduled_question(schedule_id: int) -> None`:

1. Load `ChatSchedule` row. If `enabled=False`, log and return (cron may have fired before the toggle propagated).
2. Substitute `{{since}}` and `{{now}}` in the prompt.
3. Create a fresh `ChatSession` row for the schedule's user, with `scheduled_id = schedule.id` and `title = f"Scheduled: {schedule.title} · {date}"`.
4. Call `agent.run_turn(session.id, substituted_prompt)` and consume events. Concatenate `text_delta`s; the final `message_stop` event closes the assistant text. Tool calls happen inside this loop — same catalog, same cost accounting.
5. Determine status:
   - `ok` if `message_stop` arrived with `stop_reason="end_turn"` and the final assistant text is non-empty.
   - `error` if any `error` event arrived or the agent errored. Capture the error message.
   - `timeout` if the chat agent's per-turn timeout (`CHAT_TURN_TIMEOUT_S`, default 90s) fired.
6. Compose the email:
   - **Subject:** `[Watch Question] {schedule.title} — {YYYY-MM-DD}` on success; `[Watch Question] FAILED — {schedule.title}` otherwise. The `[Watch Question]` prefix is the IMAP filter the reply poller uses — keep it stable.
   - **Body (success):** the assistant's final text, then a blank line, then `---` and the footer (see below).
   - **Body (failure):** a short "Scheduled question failed: <reason>" plus the same footer.
7. Fan out: for each address in `schedule.recipient_emails`, call `mailer.send_email(address, subject, body)`. Record per-recipient `ok` / `error: <reason>` in `last_recipient_status`. If the mailer reports no credentials at all, short-circuit the loop and set `last_status="no_email"`.
8. Set `last_status`:
   - `ok` if the agent ran cleanly and every recipient send succeeded.
   - `partial_email` if the agent ran cleanly but at least one recipient send failed (rest still got the email).
   - `error` / `timeout` if the agent itself failed (in which case the failure email is what's being fanned out).
   - `no_email` if SMTP is unconfigured — the chat session still exists and is openable in the UI.
9. Update the schedule row: `last_run_at = now`, `last_session_id = session.id`, `last_status`, `last_error`, `last_recipient_status`.

**Footer template:**

```
---
From your scheduled question: {schedule.title}
Open the conversation to see what the agent looked at, or ask a follow-up:
{BASE_URL}/chat/{session.id}

Reply to this email to ask a follow-up question — the agent will reply
to you (only). [ref:s{session.id}]

Manage this schedule: {BASE_URL}/chat/schedules
```

`BASE_URL` comes from env (`PUBLIC_BASE_URL`, falling back to `http://localhost:8000`). Already used elsewhere if set; if not, document it as required for scheduled-question links to be useful.

The `[ref:s{session.id}]` token is the canonical routing key for replies. Email clients quote the original body when replying, so the token comes back to us inside the quoted block. The poller extracts it with a single regex (`\[ref:s(\d+)\]`).

## "Run now" semantics

The `POST /api/chat/schedules/{id}/run` endpoint fires the same `run_scheduled_question` immediately, off the cron clock. It does **not** advance `last_run_at` — wait, yes it does: the `{{since}}` window must reflect the most recent run regardless of how it was triggered, otherwise the next cron run will double up. So "Run now" is indistinguishable from a cron run except in how it was triggered.

This means: if you "Run now" right before the cron fires, the cron run will see a near-empty since-window. That's correct behaviour and matches user intent ("I just got the latest, don't send me a duplicate in 5 minutes").

## Settings & admin placement

The current `/settings/keys` page (`app/templates/settings_keys.html`) lumps all `MANAGED_KEYS` together, including `GMAIL_USER` and `GMAIL_APP_PASSWORD`. That mixes two concerns: **engine API keys** (Anthropic, Brave, Tavily, Voyage, Gemini, ZenRows, ScrapingBee, Serper) — model and search infrastructure the agent needs to think — versus **notification transport credentials** — how email leaves and arrives. They should live in different tabs.

**Engine API keys stay at `/settings/keys`.** No change to that page beyond filtering the Gmail entries out.

**Email transport moves to a new admin tab `/admin/notifications`.** Reasons:

- It's a system-wide concern (one mail account services the whole instance), not a per-user setting → admin role, not viewer/analyst.
- It groups naturally with future notification-related controls (test-send, IMAP poll status, schedule-wide throttles, Slack webhook if we add one).
- Splits responsibility cleanly: `/settings/*` is "configure the engine"; `/admin/*` is "operate the deployment."

### `/admin/notifications`

A new admin-only tab in the top nav, sibling to Competitors, Skills, Search quality, Users (`app/templates/base.html` nav block).

Sections in v1:

1. **Mail account.**
   - Two managed-key rows (`GMAIL_USER`, `GMAIL_APP_PASSWORD`) reusing the existing inline-edit pattern from [app/templates/settings_keys.html](app/templates/settings_keys.html). Same backend (`/api/settings/keys/{name}` PUT / DELETE) — no new persistence, just a different page renders the rows.
   - A short "How to get an App Password" help block (link to Google's docs, one-line note that 2FA is required on the Gmail account).
   - **Test send.** Button + email-address field. POSTs to `/api/admin/notifications/test-send`, which calls `mailer.send_email(addr, "[Watch] Test", "...")` and renders the result inline. Lets the admin confirm credentials end-to-end without waiting for a scheduled run.

2. **Inbound polling.**
   - Status line: "Reply poller: enabled · last successful poll: 2 min ago · last error: —" (or the last error if any). Reads APScheduler job state and a small in-memory counter updated by `poll_replies()`.
   - Single env-key row for `CHAT_REPLY_POLL_MINUTES` (default 5) so an admin can tune the cadence. Add this to `MANAGED_KEYS` with a sensible numeric validator.
   - "Poll now" button that triggers `poll_replies()` synchronously and shows the result count. Useful for debugging "why didn't my reply land?".

3. **Existing digest mailer settings (future).** When the legacy digest config (`config["team"]`, `can_reply`, `competitors`) eventually moves out of `config.json` and into the DB, this tab is its home. Out of scope for v1 — left as a "where this grows" note.

### `app/env_keys.py` change

`MANAGED_KEYS` is unchanged structurally (same dict, same storage path). Add a small **category** field so the rendering layer knows where each key belongs:

```python
MANAGED_KEYS: dict[str, dict] = {
    "ANTHROPIC_API_KEY":  {"category": "engine", "description": "..."},
    ...
    "GMAIL_USER":         {"category": "notifications", "description": "..."},
    "GMAIL_APP_PASSWORD": {"category": "notifications", "description": "..."},
    "CHAT_REPLY_POLL_MINUTES": {"category": "notifications", "description": "...", "type": "int"},
}
```

`status()` gains a `category` filter argument: `status(category="engine")` for `/settings/keys`, `status(category="notifications")` for `/admin/notifications`. Default (no filter) returns everything — preserves the current API for any other caller.

### `/settings/keys` change

Filter to `category="engine"`. The page's intro text drops the "Gmail keys" mention. Add a one-line breadcrumb at the top: "Looking for email settings? Admin → Notifications." with a link, so anyone with the old mental model finds it immediately. Same pattern as the existing breadcrumb on `/settings` ("Skills moved to Admin → Manage skills" in `app/templates/settings_home.html`).

### Auth

- `/admin/notifications` and `/api/admin/notifications/*` require admin role (same gate as the other `/admin/*` routes).
- `/api/settings/keys/{name}` already gates writes — no change. Both pages share that endpoint.

### Acceptance criteria addendum

- `/admin/notifications` renders for admin users only (non-admins get the same redirect/403 as other admin pages).
- The Mail account section reads/writes `GMAIL_USER` and `GMAIL_APP_PASSWORD` via the existing managed-keys API.
- Test-send actually sends an email and reports the SMTP outcome.
- Reply poller status reflects the live APScheduler job and the most recent `poll_replies()` outcome.
- `/settings/keys` no longer lists Gmail keys, includes the breadcrumb to the new tab, and is otherwise unchanged.

## Reply-to-converse

Replies arrive over IMAP (same Gmail account that sends, same `GMAIL_USER` / `GMAIL_APP_PASSWORD`). A scheduler job polls the inbox every 5 minutes (`CHAT_REPLY_POLL_MINUTES`).

### Poll loop (`app/chat/replies.py::poll_replies`)

1. IMAP `SELECT inbox`; search for `(UNSEEN SUBJECT "[Watch Question]")` — narrow to our subject prefix so we don't paw through unrelated mail.
2. For each match:
   - Skip if the subject doesn't start with `Re:` (initial sends are noise to this poller).
   - Extract the sender address (`From:` header, parsed) and lowercase it.
   - Look up the user: `users.email == sender` (case-insensitive). If no match, mark the message `\Seen`, log `ignoring reply from unrecognised address: <addr>`, continue. *No email back* — that path is a spoofing megaphone.
   - Extract the body (text/plain preferred, fall back to text/html stripped via `mailer._strip_html`). Run it through the same quote-stripping that `mailer.check_for_replies` does (drop `>` lines and "On … wrote:" tails).
   - Extract the ref token: `re.search(r"\[ref:s(\d+)\]", body_or_quoted)`. Search the *full* body including the quoted portion — that's where the token lives after the user types their reply on top.
   - If no token: mark `\Seen`, log, continue. (We don't want to invent a session for a reply we can't route.)
   - Look up the session by id. If it doesn't exist or is archived: mark `\Seen`, log, continue.
   - Route per the rules below.
3. Mark the message `\Seen` regardless of routing outcome — never re-process the same reply.

### Routing rules

Two cases, distinguished by who owns the referenced session:

**Case A — referenced session is the original scheduled run (or someone else's fork).** The replier is *not* the session owner. We **fork**:
1. Create a new `ChatSession` owned by the replier, `forked_from_id = referenced_session.id`, `title = f"Re: {referenced_session.title}"`.
2. Copy every message row from the referenced session (`role`, `content`, `tool_payload`, etc.) into the new session in order, with new ids. Tool calls and their results carry over so the agent has full context. Cost ledger on the new session starts at zero (the historical tokens are not the replier's spend).
3. Append the reply text as a new `user` `chat_messages` row.
4. Run `agent.run_turn(new_session.id, reply_text)` to completion. The agent sees the prior conversation plus the reply and answers in context. Tool catalog is filtered by *the replier's* role.
5. Email the assistant's final text back to **the replier only** (subject: `Re: [Watch Question] {original_title}`, footer rebuilt with `[ref:s{new_session.id}]` so the next reply continues in the fork rather than re-forking).

**Case B — referenced session is already a fork owned by the replier.** Continue in place:
1. Append the reply text as a new `user` row on the existing forked session.
2. Run a turn.
3. Email back, with the footer's ref token still pointing at the same forked session.

This means a back-and-forth thread (reply, agent reply, reply again, …) all flows through one forked session — the user's personal copy of that scheduled question.

### Boundaries

- A replier other than the schedule owner cannot trigger writes — same role-gating as interactive chat. A viewer who replies sees only read tools.
- A reply to a *failure* email follows the same routing rules — the forked session inherits the partial state. Useful for "the agent broke, but I want to dig into what happened".
- Replies don't update `schedule.last_run_at`. The `{{since}}` window is bound to the schedule's own runs, not to follow-up conversations.
- Per-user concurrency cap (one in-flight turn per user) applies. If the replier is already mid-conversation interactively, the reply waits up to 30s, then errors with an email back.
- IMAP errors are logged but don't crash the scheduler. Next poll tick retries.

## UI

### `/chat/schedules` (index)

```
Chat / Schedules                                [ + New schedule ]

  ┌────────────────────────────────────────────────────────────────────┐
  │ ✓ Weekly competitor summary       Mon 8am   ok · 2 days ago        │
  │   "Summarise what's new with our top 5 competitors since…"         │
  │   → simon@…, anna@…, dev@… (3)   ✓ all delivered                   │
  │   2 follow-up replies this week                                     │
  │   [ Run now ]  [ Edit ]  [ Disable ]  [ × ]                        │
  ├────────────────────────────────────────────────────────────────────┤
  │ ⚠ Pricing watch                   Daily 8am   partial_email · 4h   │
  │   "Any pricing changes mentioned in the last 24h since…"           │
  │   → simon@…, ext@…  (1 send failed: smtp 550 ext@…)                │
  │   [ Run now ]  [ Edit ]  [ Disable ]  [ × ]                        │
  ├────────────────────────────────────────────────────────────────────┤
  │ ○ ATS deep-dive                   Monthly 1st  disabled            │
  │   "Pull the latest ATS deep-research and call out anything…"       │
  │   → strategy@…                                                      │
  │   [ Run now ]  [ Edit ]  [ Enable ]  [ × ]                         │
  └────────────────────────────────────────────────────────────────────┘
```

Status icons: ✓ last run ok · ⚠ last run failed or partial · ○ disabled · — never run.

The "follow-up replies this week" line counts forked sessions where `forked_from_id IN (sessions of this schedule) AND created_at > now - 7d`. Cheap query, useful signal.

### `/chat/schedules/new` and `/edit/{id}` (form)

- **Title** (text, required, ≤255 chars).
- **Prompt** (textarea, required). Help text: "Use `{{since}}` to refer to the time of the previous run — the agent will filter findings to that window."
- **Schedule** — a small radio group with presets (Daily 8am, Weekly Monday 8am, Monthly 1st 8am, Custom). Custom reveals a raw cron text field with inline validation.
- **Recipients** — a tag-input field (or one-email-per-line textarea — keep it simple). Defaults to `[current_user.email]`. Each entry validated for email shape and capped at `CHAT_SCHEDULE_MAX_RECIPIENTS` (25). A small inline note next to each entry: "✉︎ can reply" if the address matches an existing user, "✉︎ send-only" otherwise. Helps the schedule owner understand reply behaviour at a glance.
- **Enabled** (checkbox, default true on create).
- Save button. On save, calls `app.scheduler.register_schedule(id)` so the cron updates without a process restart.

### From `/chat` index

The header gains a small "Schedules" link next to "+ New chat" so users discover the feature.

### From `/chat/{id}` for a scheduled session

Header badge: `from schedule: {title}` linking to `/chat/schedules/{id}/edit`. The user can ask follow-up questions in the same thread; those new turns are normal chat turns and don't trigger more emails.

## Auth

- All `/chat/schedules*` routes require an authenticated user.
- Each route checks `schedule.user_id == current_user.id` and 404s otherwise (don't leak existence). Recipients listed on a schedule do **not** get UI access to it — only the owner edits.
- The cron entrypoint runs server-side without an HTTP request; it loads the schedule's owner from the row and uses *that* user's role for the agent's tool-catalog filter. A viewer's scheduled question sees only read tools.
- **Reply-as-user authentication is by sender email, full stop.** The IMAP poller compares the parsed `From:` address against `users.email` (case-insensitive) and uses the matched user as the actor. This is exactly as strong as the user's email account itself. Risks:
  - **Spoofed `From:` headers.** SPF/DKIM/DMARC on inbound to the watch's Gmail account is the mitigation; Gmail's spam filter is the de-facto enforcer. We do not add a separate signed-token-in-the-email layer in v1 because it adds friction (users can't just hit Reply) for a threat that's largely already handled by the mail provider. If someone runs Watch on infrastructure where inbound DMARC isn't enforced, they should not enable scheduled questions.
  - **Forwarded emails.** If Alice forwards a scheduled-question email to Bob, and Bob replies, the `From:` is Bob's. If Bob is a user, his reply forks normally. If Bob is not, it's ignored. This is the desired behaviour.
- **Writes from a scheduled or replied run are auto-cancelled.** The agent may emit a write `tool_use` (e.g. `run_market_synthesis`), but with no human present to click Confirm, the tool result is short-circuited to `{"cancelled": true, "reason": "scheduled run, no interactive confirmation"}` and the agent is told to continue without that side-effect. Applies equally to forks from replies — the replier is "present" by email but not by browser. v1 does not support pre-approving writes. Add a per-schedule `auto_approve_writes` flag in a follow-up if a real need shows up.

## Cost & rate-limit discipline

- A scheduled run obeys the same per-session caps as a normal chat session: tool budget, turn timeout, soft/hard cost cap.
- **Per-user concurrent run cap.** If the user already has an in-flight chat turn (interactive or scheduled), a fresh scheduled run waits up to 30s for it to clear, then logs "skipped — concurrent turn in progress" and emails a short notice. The skip is recorded as `last_status="error"` with a clear `last_error`.
- **Daily run cap per user.** Soft cap of 50 scheduled runs per user per day (configurable via `CHAT_SCHEDULE_DAILY_CAP`). Past the cap, runs are skipped with `last_status="error"` and an email. Stops a misconfigured "every minute" cron from burning the budget overnight.
- **Cron sanity validation.** On save, reject crons that fire more often than every 10 minutes (`CHAT_SCHEDULE_MIN_INTERVAL_MINUTES`). Form-level validation, not a database constraint.

## Testing

**Scheduling and run loop:**
- Create a schedule with cron `*/10 * * * *`, prompt `"Hello, the time is {{now}}."`, recipients = `[your_email, teammate_email]`. Wait for the next 10-minute boundary; an email arrives at *both* addresses with the same agent reply, same chat link, same ref token.
- Create a schedule with `{{since}}` in the prompt. First run: agent sees a 7-days-ago timestamp and produces output. Second run: agent sees the first run's `created_at` and the output reflects that narrower window.
- "Run now" on a schedule fires a session immediately and fans out to all recipients. Open the linked `/chat/{id}` — full conversation visible with tool cards.
- Ask a follow-up *in the chat UI* (not by email) on a scheduled-run session: a new turn runs interactively, no email is sent. Cost rolls up on the same session.
- Disable a schedule: cron stops firing within ~1 second. Re-enable: it's back.
- Delete a schedule: row removed, past `ChatSession` rows remain accessible at `/chat`. Forked sessions belonging to other users also remain.
- Trigger a write: a scheduled prompt that asks the agent to run `run_market_synthesis` results in a `cancelled=true` tool_result; the email's body explains the agent tried but couldn't.
- Trigger a real failure (kill `ANTHROPIC_API_KEY` mid-run): scheduler catches, status=error, failure email goes to all recipients, dashboard shows ⚠.
- Submit a cron that fires every minute — form rejects with "minimum interval is 10 minutes". Submit 30 recipient emails — form rejects with "max 25 recipients per schedule".
- With `GMAIL_USER` unset, run a schedule: chat session runs to completion, status=`no_email`, dashboard shows a "Set Gmail credentials" banner linking to `/settings/keys`. No fanout attempted.
- Restart the app: existing schedules re-register on boot; the recurring `chat_reply_poll` job is also registered.
- Try to load another user's schedule by id — 404.

**Multi-recipient fanout:**
- Schedule with two recipients where one is a known-bad address (e.g. mistyped). The valid recipient gets the email; the invalid one fails. Schedule status = `partial_email`, `last_recipient_status` records `{"good@…": "ok", "bad@…": "error: …"}`. Dashboard shows the per-recipient breakdown.
- All recipients fail (e.g. SMTP outage): status = `partial_email` (or `error` — pick one and stick with it; spec says `partial_email` covers ≥1 failure). Visible on dashboard.

**Reply-to-converse:**
- Reply from a recognised recipient (matches `users.email`): within 5 minutes (one poll cycle), a forked `ChatSession` appears in the replier's `/chat` list with `forked_from_id` set to the original. The replier gets an email back with the agent's answer. The footer's ref token now points at the forked session.
- Reply *to that reply email* (continuing the thread): no new fork is created; the existing fork gets a new turn. Replier gets another email back.
- Reply from an unrecognised address: nothing happens (no email back, no session). Server logs the ignore.
- Reply with no `[ref:s…]` token in the body or quoted body (e.g. someone composes a fresh email with the right subject prefix): ignored, logged.
- Reply that references a deleted/archived session: ignored, logged.
- Reply from a viewer-role user that asks the agent to run a write: agent has no write tools in its catalog (role-filtered) and explains it can't.
- Reply from an analyst-role user: write tools are in the catalog but auto-cancel — same behaviour as scheduled runs. Email back explains.
- Two replies from the same user, ~simultaneous (one from web, one from another email): per-user concurrency cap holds; second waits then errors with an email.
- IMAP credentials misconfigured: poll job logs the error, doesn't crash; subsequent polls retry.

## Acceptance criteria

1. `chat_schedules` table exists with a working migration; `chat_sessions.scheduled_id` and `chat_sessions.forked_from_id` columns added in the same migration.
2. `/chat/schedules` lists the caller's schedules with status, last-run timestamp, per-recipient send breakdown, follow-up reply count, and per-row actions (Run now, Edit, Enable/Disable, Delete).
3. Creating or editing a schedule registers/updates the underlying APScheduler job without an app restart.
4. When a cron fires, a `ChatSession` is created with `scheduled_id` set, the agent runs one turn end-to-end, and the email fan-out attempts every address in `recipient_emails`. Per-recipient outcome is recorded in `last_recipient_status`.
5. `{{since}}` and `{{now}}` are substituted correctly; first runs default `{{since}}` to 7 days ago.
6. Scheduled and replied runs that emit a write `tool_use` short-circuit it with `cancelled=true` and the agent continues; the resulting answer is emailed normally.
7. Failures email *all recipients* with a clear "FAILED" subject and the error blurb; the dashboard reflects status=error.
8. Cron expressions that violate the minimum-interval guard, and recipient lists exceeding `CHAT_SCHEDULE_MAX_RECIPIENTS`, are rejected at form save with clear errors.
9. The per-schedule `last_session_id` link from the dashboard opens the full conversation at `/chat/{id}`, where follow-ups can be asked interactively.
10. Deleting a schedule removes the row and unregisters the cron job; the historical `ChatSession` rows (and any forked-reply sessions) remain accessible.
11. The cost ledger rolls up per session as normal; scheduled-run sessions appear in the owner's spend totals; forked-reply sessions appear in the replier's totals.
12. Boot of `app/scheduler.py` re-registers all enabled schedules **and** the `chat_reply_poll` job within ~1s of `start()`.
13. The reply poller fires on its interval, scans inbox for `[Watch Question]` replies, and routes recognised-sender replies into a forked or continued session; unrecognised senders and missing/invalid ref tokens are silently ignored (logged, not emailed back).
14. A reply forks the original session into a new session owned by the replier with `forked_from_id` set; subsequent replies in the same thread continue in that fork rather than re-forking.
15. Reply-triggered runs use the *replier's* role for tool-catalog filtering, not the schedule owner's.

## What this unblocks

- **Per-schedule auto-approve for writes.** Once the read-only flow is proven, add `auto_approve_writes` so a schedule (or a recognised reply) can run e.g. `run_market_synthesis` without a human click.
- **HTML email body.** Swap the plain-text template for an HTML one (the mailer already supports a `text/html` part). Useful for findings-by-competitor lists with clickable urls.
- **Per-recipient filtering.** Each recipient gets a body filtered to the competitors they care about (mirrors the existing digest's per-team-member filter in `mailer.py::send_digest_to_team`). Requires recipients to be linked to user records, not free-form emails.
- **Group threading.** A reply that *all* recipients see, with the agent moderating. Today each reply forks privately. A "team thread" mode would keep the conversation shared.
- **Schedule sharing.** Read-only template URL so a teammate can clone a useful schedule into their own account.
- **Per-schedule model override.** A "use the cheap model for this digest" knob, once we see which schedules don't need the strongest model.
- **Signed reply tokens.** If we ever run on infrastructure where inbound DMARC isn't enforced, sign the `[ref:s…]` token with an HMAC of the recipient's address so a spoofed `From:` can't reach a session that wasn't sent to that address.
