# Spec — Chat (agentic console over the watch)

**Status:** Draft
**Owner:** Simon
**Depends on:** the existing FastAPI routes (`app/routes/*`) and skill files. No new adapters; uses the Anthropic key already wired for Haiku classification.
**Unblocks:** a single conversational surface for asking the watch questions, pulling reports, and triggering the same actions the UI buttons trigger — without learning the page hierarchy.

## Purpose

Today, getting an answer out of Competitor Watch means knowing *where* to look — Stream for raw findings, Competitors for the per-name profile, Market for the digest, Runs for what fired when. That's fine when you know the question. It breaks down when the question is "what changed with Ashby this week and is it something we should care about?" — that's three pages and a synthesis you do in your head.

**Chat** is one box that can answer those questions and act on the answer. A Claude-backed agent with read-tools over our own data (findings, reports, syntheses, competitor profiles, runs) plus a curated set of write-tools that wrap the buttons we already ship (run a market digest, kick off deep research, save a stream filter). It fetches what it needs on demand via tools — no preloaded mega-context — so the conversation stays fast and the model only sees what's relevant to the turn.

One row per session, one row per message, persisted in SQLite so conversations are resumable and shareable. Lives at `/chat` under the Stream nav group, alongside the existing surfaces it summarizes.

## Non-goals

- **Replacing the existing pages.** Stream / Competitors / Market / Runs all stay. Chat is a different read mode (ask a question, get a synthesized answer with citations to the underlying rows), not a re-skin of the data.
- **A general-purpose Claude playground.** The system prompt scopes the agent to this product. It will refuse off-topic asks and redirect to the watch tools.
- **Browser-side LLM calls.** The Anthropic SDK runs server-side. Browser sees server-sent events.
- **Agentic write-actions beyond the v1 catalog.** v1 wraps three existing buttons (run market digest, run deep research for a competitor, run market synthesis). Anything destructive (delete competitor, edit users, rotate API keys) stays out of the tool catalog. We add tools deliberately, one at a time, with human confirmation gates.
- **Multi-user shared sessions.** Each session belongs to one user. Sharing is a future feature once we know the shape.
- **Custom-skill authoring from chat.** Skills are still edited at `/admin/skills`. The agent can *read* skill bodies as a tool but doesn't write them.
- **Memory across sessions.** Each session starts fresh. The agent has no recall of prior conversations beyond what the user pastes into the new turn. Cross-session memory is a future spec.
- **Streaming through HTMX SSE extension.** Standard SSE endpoint + a small JS handler — fewer moving parts than wiring htmx-sse for a feature that doesn't fit the partial-swap model.

## Design principles

1. **Tool-fetch over context-stuff.** The model gets a small system prompt and the conversation. Everything else (findings, reports, profiles) comes from tool calls the model makes during the turn. Keeps token cost flat with conversation length and gives us a clean audit trail of what the model actually looked at.
2. **One curated catalog, hand-picked.** Tools are explicit Python functions in `app/chat/tools.py` that wrap existing services or routes. Auto-exposing every FastAPI route is rejected — the surface is too large and the schemas leak admin-only routes.
3. **Read first, write gated.** v1 catalog is mostly read. The three write tools (`run_market_digest`, `run_deep_research`, `run_market_synthesis`) are auth-gated to admin/analyst and emit a confirmation step in the chat before firing.
4. **One row per session, one row per message, append-only.** Mirrors the rest of the codebase. No edits, no soft-deletes in the happy path.
5. **SSE for the assistant turn.** Streaming text + tool-call events come down a single `text/event-stream` connection per turn. The browser appends as events arrive. Page is server-rendered at load; only the live turn streams.
6. **Anthropic SDK with prompt caching + tool use.** System prompt + tool catalog are cache-eligible (they don't change within a session). Each turn pays full cost only for the new user message + assistant output.
7. **Small, citable answers.** The system prompt instructs the model to cite the rows it read by id (e.g. `(finding #4821)`, `(competitor: Ashby)`) and to keep responses skimmable.
8. **Fail soft on missing keys.** No `ANTHROPIC_API_KEY` → the chat page renders a "Add a key" nudge pointing at `/settings/keys`. Same pattern as Spec 04.
9. **Per-session cost ledger.** Every turn's input/output tokens and cost are written to the message row and rolled up on the session row. Visible in the chat header so the user always sees what the conversation has cost.

## Where it lives

- **Models + migration**
  - `app/models.py` — new `ChatSession` and `ChatMessage` classes (shapes below).
  - `alembic/versions/<new>_chat_sessions.py` — create both tables with indexes.
- **Agent core**
  - `app/chat/__init__.py` — package marker.
  - `app/chat/agent.py` — the turn loop. `run_turn(session_id, user_text) -> AsyncIterator[Event]` yields SSE-shaped events (`message_start`, `text_delta`, `tool_use`, `tool_result`, `message_stop`, `error`). Wraps Anthropic SDK with tool use loop, caching, and persistence.
  - `app/chat/tools.py` — the curated tool catalog. Each tool is `(name, description, input_schema, handler)`. `handler(db, user, **kwargs) -> dict` is a sync function that returns JSON-serialisable data the model can read.
  - `app/chat/system_prompt.md` — the system prompt as a skill-file. Placeholders `{{our_company}}`, `{{our_industry}}`, `{{tool_catalog}}`. Editable at `/admin/skills` like the other skills.
  - `app/skills.py::KNOWN_SKILLS` — register `chat_system`.
- **Routes**
  - `app/ui.py` — `GET /chat` (session list + new-chat form), `GET /chat/{id}` (full conversation page).
  - `app/routes/chat.py` — new module:
    - `POST /api/chat/sessions` → create session, redirect to `/chat/{id}`.
    - `POST /api/chat/{id}/messages` → submit a user message, returns SSE stream of the assistant turn.
    - `GET /api/chat/{id}/messages` → JSON list, used for resume after reload.
    - `DELETE /api/chat/{id}` → soft-archive (status flip, not row delete).
- **Templates**
  - `app/templates/chat_index.html` — session list (sidebar) + welcome panel with "New chat" button.
  - `app/templates/chat_session.html` — full conversation page. Server-renders all prior messages; only the current/streaming turn is hydrated by the SSE handler.
  - `app/templates/_chat_message.html` — partial for one message row (user / assistant / tool-call / tool-result).
- **Static**
  - `app/static/chat.js` — small SSE handler. Opens an `EventSource` for the in-flight turn, appends `text_delta` events to the trailing assistant bubble, renders `tool_use` / `tool_result` as collapsible inline cards. ~150 lines of vanilla JS. No build step.
  - `app/static/style.css` — chat-specific section. Bump `?v=` cache-buster.
- **Nav**
  - `app/templates/base.html` — Stream nav item gains a sub-link "Chat" (or a sibling nav-item directly under Stream — see UI section). Active state when `path.startswith('/chat')`.

## Data model

```python
class ChatSession(Base):
    """One conversation, owned by one user.

    Append-only at the message level. Sessions can be archived
    (status='archived') but never row-deleted in the happy path so
    cost ledgers and tool-call audit stay intact.
    """
    __tablename__ = "chat_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )

    # Auto-derived from the first user turn ("Summarise Ashby this week")
    # or set by the user. Falls back to "New chat · {{started_at}}" until
    # the first turn names it.
    title: Mapped[str] = mapped_column(String(255), default="New chat")

    # "active" | "archived"
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)

    # The model id used for this session (frozen at creation so a model
    # swap mid-conversation doesn't silently change behaviour).
    model: Mapped[str] = mapped_column(String(64), default="claude-sonnet-4-6")

    # Rolled-up totals across all messages in the session. Updated on
    # each assistant turn so the header can render live cost.
    total_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ChatMessage(Base):
    """One message in a session. Ordered by id (monotonic per session).

    role='user' rows carry user input. role='assistant' rows carry the
    model's text. role='tool_use' rows record a tool the model invoked
    and the arguments it passed. role='tool_result' rows record what
    came back. We store tool turns as their own rows (rather than
    nesting them on the assistant message) so the audit trail and
    re-render are uniform.
    """
    __tablename__ = "chat_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="CASCADE"), index=True
    )

    # "user" | "assistant" | "tool_use" | "tool_result" | "error"
    role: Mapped[str] = mapped_column(String(16), index=True)

    # User/assistant text content. Empty for pure tool turns.
    content: Mapped[str] = mapped_column(Text, default="")

    # For role='tool_use': {"id": str, "name": str, "input": dict}
    # For role='tool_result': {"tool_use_id": str, "output": <json>, "is_error": bool}
    # Empty dict for user/assistant text.
    tool_payload: Mapped[dict] = mapped_column(JSON, default=dict)

    # Per-turn cost accounting. None on user/tool rows; populated on
    # the assistant text row that closes a turn.
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)

    # The stop reason from the SDK on assistant rows: "end_turn" |
    # "tool_use" | "max_tokens" | "stop_sequence". None on others.
    stop_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
```

**Indexes:**
- `chat_sessions(user_id, status, updated_at DESC)` — sidebar list query.
- `chat_messages(session_id, id)` — the canonical message order.
- `chat_messages(role)` — filtered audits ("show me every tool_use this week").

## Tool catalog (v1)

Each tool is a sync function in `app/chat/tools.py` registered in a `TOOLS: list[Tool]` list. `Tool` is a small dataclass: `name`, `description`, `input_schema` (JSON Schema dict), `handler(db, user, **kwargs) -> dict`, `requires_role` (one of `viewer | analyst | admin`).

The `tool_catalog` placeholder in the system prompt is rendered from this list at chat-load time so the prompt always reflects the actual tools available to the user's role.

**Read tools (v1):**

| Name | What it returns |
|------|------------------|
| `list_competitors` | All active competitors: id, name, category, threat_angle. |
| `get_competitor_profile` | One competitor's full profile: roster + 7-day finding count + latest review excerpt. |
| `search_findings` | Findings filtered by competitor, signal_type, since_days, materiality. Returns id, title, summary, url, published_at, threat_level. Paginated. |
| `get_finding` | Full finding row by id (incl. content excerpt and metadata). |
| `list_reports` | Recent market digest reports (Report rows): id, kind, title, started_at. |
| `get_report` | One report's body_md + sources. |
| `get_latest_market_synthesis` | Latest `MarketSynthesisReport`: title, body_md excerpt, inputs_meta, sources count. |
| `get_deep_research_report` | Latest `DeepResearchReport` for a competitor by id or name. |
| `list_recent_runs` | Last N runs with kind, status, duration, triggered_by. |
| `get_company_brief` / `get_customer_brief` | Latest `ContextBrief` for the requested scope. |
| `get_skill_body` | Full text of one skill (read-only). Lets the agent explain what a skill prompts for when the user asks. |

**Write tools (v1, auth-gated to admin/analyst, with confirmation):**

| Name | What it does |
|------|---------------|
| `run_market_digest` | Calls the existing `/api/runs/market-digest` flow. Returns the new run id. |
| `run_deep_research` | Calls the existing per-competitor deep research flow for a named competitor. |
| `run_market_synthesis` | Calls the existing `/api/runs/market-synthesis` flow. |

Confirmation pattern: write-tool calls go through a two-step shape — the agent emits a `tool_use` with a `confirmation_required: true` flag (computed in the handler wrapper, not the model), the UI renders a "Run this?" card with [Confirm] / [Cancel], and the actual route is called only after the user clicks Confirm. The confirmation result is fed back as the `tool_result`. Prevents the agent from auto-running expensive jobs on its own initiative.

Anything destructive (delete, role-change, key-rotation) is **not** in the v1 catalog. Add deliberately in later specs.

## System prompt (`app/chat/system_prompt.md`)

Sketch, not final:

```markdown
You are the chat agent for {{our_company}}'s competitor watch. The user
is on the {{our_company}} strategy team — smart, time-poor, wants signal
not noise.

You have read-access to our own watch data via tools, and a small set
of write tools that mirror buttons in the UI. Use the tools to answer
the user's questions; do not invent findings, reports, or numbers.

Cite the rows you read inline by id, e.g. (finding #4821) or
(competitor: Ashby). When you summarise across multiple findings, list
the ids you read at the end of the answer.

When the user asks about a competitor, lead with what's *new* — the
last 7 days of findings — then layer in older context only if the
question warrants it.

When the user asks for a market read, prefer `get_latest_market_synthesis`
over the daily digest unless they explicitly want today's read.

Don't run a write tool unless the user has explicitly asked for that
action. When you do propose a write, the system will surface a
confirmation card to the user — that's expected; just call the tool.

Tools available to you (filtered by your user's role):
{{tool_catalog}}

If a tool returns an error, surface it to the user plainly and stop —
don't retry the same call with different arguments unless you have a
clear reason to.

Refuse off-topic asks (general coding help, world knowledge unrelated
to the watch) by redirecting to what you *can* do.
```

## Turn execution

`app/chat/agent.py::run_turn(session_id, user_text)` is an async generator that yields SSE events. Pseudocode:

```python
async def run_turn(session_id, user_text):
    session = load_session(session_id)
    user = session.user
    persist user message
    yield message_start

    messages = build_anthropic_messages(session)  # all prior rows
    tools = catalog_for_role(user.role)
    system = render_system_prompt(user, tools)

    while True:
        async with anthropic.messages.stream(
            model=session.model,
            system=[{"type": "text", "text": system,
                     "cache_control": {"type": "ephemeral"}}],
            tools=tools_to_anthropic_schema(tools),
            messages=messages,
            max_tokens=4096,
        ) as stream:
            async for event in stream:
                if event.type == "content_block_delta" and delta.type == "text_delta":
                    yield text_delta(delta.text)
                # accumulate tool_use blocks as they arrive
            final = await stream.get_final_message()

        persist assistant message (text + usage + stop_reason)
        update session totals

        if final.stop_reason != "tool_use":
            yield message_stop
            return

        for block in final.content of type tool_use:
            persist tool_use row
            yield tool_use(block)
            if tool requires confirmation:
                yield confirmation_pending(block)
                wait_for_confirmation(block.id)  # SSE round-trip via a
                                                  # POST /api/chat/{id}/confirm/{tool_use_id}
            try:
                result = run_handler(block.name, block.input, db, user)
            except Exception as e:
                result = {"error": str(e)}
                is_error = True
            persist tool_result row
            yield tool_result(block.id, result)

        messages = rebuild_messages_from_db(session)
        # loop continues with the tool result fed back to the model
```

**Tool loop bound:** at most 8 tool calls per turn (configurable via `CHAT_MAX_TOOL_CALLS`). On the 9th, the loop short-circuits with an assistant message "I've used my tool budget for this turn — let me know how you want to continue" so a runaway agent can't burn the whole budget on one question.

**Hard timeout:** 90s per turn (configurable via `CHAT_TURN_TIMEOUT_S`). On timeout, persist an error row and yield `error`.

**Resume on reload:** the chat page server-renders all prior messages plus any `assistant` row whose `stop_reason` is `null` (= turn was interrupted). The UI shows that as "Turn was interrupted — [Retry]" and posts back to re-trigger the assistant turn from where the messages left off.

## SSE event shapes

```
event: message_start
data: {"message_id": 4821}

event: text_delta
data: {"text": "Here's what's new with Ashby..."}

event: tool_use
data: {"id": "toolu_abc", "name": "search_findings",
       "input": {"competitor": "Ashby", "since_days": 7}}

event: confirmation_pending
data: {"tool_use_id": "toolu_xyz", "name": "run_deep_research",
       "input": {"competitor": "Ashby"}, "summary": "Run deep research for Ashby (~5–20 min, ~$3–10)"}

event: tool_result
data: {"tool_use_id": "toolu_abc", "output": {"results": [...]}, "is_error": false}

event: message_stop
data: {"stop_reason": "end_turn", "usage": {...}, "cost_usd": 0.012}

event: error
data: {"message": "..."}
```

Browser handler (`chat.js`) maintains the trailing assistant bubble in the DOM and updates per event. Tool-use events render as a collapsed `<details>` card under the assistant text. Confirmation cards render with the [Confirm] / [Cancel] buttons that POST to the confirm endpoint.

## UI

### Nav placement

A new **Chat** sub-item under Stream. Single nav item directly below "Stream" with a different icon. Active when `path.startswith('/chat')`. Stream itself is unchanged.

### `/chat` (index)

```
Chat                                                [ + New chat ]

  ┌─ Recent ─────────────────────────────────────────┐
  │ ▸ Ashby positioning shifts · 2h ago             │
  │ ▸ Q2 ATS pricing read · yesterday               │
  │ ▸ Indeed Indeed Indeed... · 3 days ago          │
  │ ▸ ...                                           │
  └──────────────────────────────────────────────────┘

  Welcome panel: a short blurb about what the agent can do, with
  three click-to-start example prompts:
    "Summarise Ashby this week."
    "Which competitors are accelerating right now?"
    "Run a fresh market synthesis."
```

Clicking a prompt creates a new session (POST `/api/chat/sessions`) with that prompt as the first user message, and redirects to `/chat/{id}`.

### `/chat/{id}` (conversation)

```
< back to Chat              "Ashby positioning shifts"     ⓘ $0.014 · 12 tools

  ┌─ Conversation ───────────────────────────────────┐
  │ user:                                            │
  │   What's new with Ashby this week?              │
  │                                                  │
  │ assistant:                                       │
  │   Three things landed this week (finding #4821, │
  │   #4830, #4842):                                 │
  │   1. ... 2. ... 3. ...                           │
  │   ▸ search_findings(competitor=Ashby, since=7d) │
  │   ▸ get_competitor_profile(name=Ashby)          │
  │                                                  │
  │ user:                                            │
  │   Run deep research on them.                    │
  │                                                  │
  │ assistant:                                       │
  │   [confirmation card]                            │
  │     Run deep research for Ashby?                │
  │     ~5–20 min, ~$3–10                            │
  │     [ Confirm ]  [ Cancel ]                     │
  │                                                  │
  └──────────────────────────────────────────────────┘
  ┌─ [textarea]                       [ Send ⏎ ] ────┐
  └──────────────────────────────────────────────────┘
```

- Header: session title (click-to-rename), live cost, tool-call count.
- Each assistant message: text + a collapsed `<details>` per tool call showing name, input, and (when ready) result. Helps debug "what did the agent actually look at".
- Confirmation cards: inline, modal-less. Confirm POSTs to `/api/chat/{id}/confirm/{tool_use_id}`; Cancel posts the same with `cancelled=true`.

### Empty / error states

- **No `ANTHROPIC_API_KEY`** → page renders a "Add a key" nudge linking to `/settings/keys`. New chat button hidden.
- **First message in a fresh session** → shows the welcome panel content above the input.
- **Turn errored** → red bubble with the error message and a "Retry" button.

## Auth

- `GET /chat`, `GET /chat/{id}` require any authenticated user. Sessions are user-scoped — listing returns only the caller's sessions; loading a session checks `session.user_id == current_user.id` and 404s otherwise.
- `POST /api/chat/sessions` and `POST /api/chat/{id}/messages` require the same.
- The write-tool catalog filter (`requires_role`) gates `run_market_digest` / `run_deep_research` / `run_market_synthesis` to admin or analyst only — viewers will literally not see those tools in the model's tool list.
- The confirmation endpoint re-checks role on click. Belt and braces.

## Cost & rate-limit discipline

- **Per-session cost cap.** Soft warn at $1, hard stop at $5 per session (configurable via `CHAT_SESSION_COST_WARN_USD`, `CHAT_SESSION_COST_HARD_USD`). Past the hard cap the session is read-only — user can fork to a new session.
- **Per-user concurrency cap.** One in-flight turn per user. A second `POST /messages` while one is streaming returns 409 Conflict.
- **Prompt caching always on.** System prompt + tool catalog get `cache_control: ephemeral`. Cost ledger reads the SDK's cache-read token count and prices it correctly.
- **Tool result truncation.** Each tool handler caps its output at 8KB serialised JSON. Bigger payloads are summarised to id-list + count and the model is told it can re-call with narrower filters.

## Testing

- Open `/chat` with no sessions. Welcome panel + example prompts render. Click an example → new session created, redirect to `/chat/{id}`, agent's first turn streams in.
- With `ANTHROPIC_API_KEY` unset, the chat page shows the key-missing nudge. Stream nav item still visible.
- Send "What's new with Ashby this week?" — agent calls `search_findings` and `get_competitor_profile`, streams an answer with `(finding #...)` citations.
- Send "Run deep research on Ashby" — agent emits a confirmation card. Click Confirm → tool fires, returns the new run id, agent acknowledges. Click Cancel on a follow-up — tool short-circuits with `cancelled=true`, agent acknowledges and asks for direction.
- Reload `/chat/{id}` mid-stream — page re-renders all prior messages, the interrupted assistant row shows a Retry button. Click Retry → the turn resumes from the last persisted state.
- Hit the per-turn tool budget (8 calls) by asking a deliberately broad question — assistant sends the budget-hit message and stops cleanly.
- Hit the hard cost cap on a session — `POST /messages` returns 402 with a "session over budget" message; UI shows the cap banner and disables the input.
- Try to load another user's session by id — 404, not 403 (don't leak existence).
- As a viewer, send "Run a market synthesis" — agent's tool list doesn't include the write tool, so it explains it can't run that and suggests asking an admin.
- Tool returns >8KB JSON — handler truncates and includes a `truncated: true` field; agent re-calls with narrower filter.
- Cache hit rate: send a 5-turn conversation, check the message rows — turns 2–5 should record a non-zero `cache_read_tokens`.

## Acceptance criteria

1. `/chat` renders for any authenticated user with the session list and a working "New chat" flow. `/chat/{id}` renders the full server-rendered conversation.
2. Clicking "New chat" or an example prompt creates a `ChatSession` row and redirects to `/chat/{id}`.
3. Submitting a message persists a `user` `ChatMessage`, opens an SSE stream, and yields `text_delta` events that the browser appends to the trailing assistant bubble.
4. Tool calls are persisted as `tool_use` + `tool_result` rows and rendered inline as collapsed cards. Read tools execute without confirmation; write tools surface a confirmation card and only fire after Confirm.
5. The model is given exactly the tools allowed for the caller's role; viewers cannot see write tools at all.
6. Per-turn tool budget and timeout are enforced and visible to the user when hit.
7. Per-session cost is updated on each assistant turn, shown in the header, and enforced at the soft/hard caps.
8. Reloading mid-stream restores all prior messages and offers a Retry on interrupted turns.
9. Missing `ANTHROPIC_API_KEY` shows a key-missing nudge; the rest of the app is unaffected.
10. The system prompt is editable at `/admin/skills` (registered as `chat_system`).
11. CSS cache-buster bumped; new chat-specific styles isolated under `.chat-*` selectors so no other page is visually affected.
12. `/runs` is unaffected; chat-driven write-tool runs appear there with their normal `kind` (`market_digest`, `deep_research`, `market_synthesis`) and a `triggered_by="chat:{session_id}"` value so we can audit what chat caused.

## What this unblocks

- **Cross-session memory.** Once we see what users actually ask, distill durable facts into a per-user memory file the system prompt loads on session start.
- **Sharing.** Read-only `/chat/{id}/share` URL for handing a finished investigation to a teammate. Same row, different ACL check.
- **More tools, deliberately.** Save filter, snooze finding, edit competitor threat angle, schedule a one-off scan — each with the same confirmation pattern.
- **Voice / mobile.** SSE + a thin client is portable; a mobile shell or a voice loop becomes a UI swap, not a re-architecture.
- **Auto-naming + auto-tagging of sessions.** A small post-turn job that titles untitled sessions from the first user message and tags them by the competitors mentioned. Nice-to-have.
- **Replay / audit.** Every tool call is a row. Building "what did chat do this week?" or "show me every write the agent triggered last month" is a single SQL query.
