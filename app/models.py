from datetime import datetime
from sqlalchemy import String, Integer, Float, DateTime, Text, JSON, ForeignKey, Boolean, UniqueConstraint, Index, text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .db import Base


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32), default="admin")  # admin, analyst, viewer
    # Bcrypt hash. NULL = legacy/stub user that cannot log in (e.g. the
    # pre-auth admin@local row that FKs point at). Real users always have one.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Saved filter to auto-apply when /stream loads with no query params.
    # ON DELETE SET NULL so removing the underlying filter doesn't strand a stale id.
    default_filter_id: Mapped[int | None] = mapped_column(
        ForeignKey("saved_filters.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AuthSession(Base):
    """Server-side session. The cookie carries only the opaque token; all state
    lives here so sessions are revocable (delete the row = immediate logout).
    token is the primary key so the cookie lookup is a single indexed hit."""
    __tablename__ = "auth_sessions"
    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Competitor(Base):
    __tablename__ = "competitors"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="manual")  # manual, discovered
    discovered_date: Mapped[str | None] = mapped_column(String(32), nullable=True)
    threat_angle: Mapped[str | None] = mapped_column(Text, nullable=True)
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    subreddits: Mapped[list] = mapped_column(JSON, default=list)
    careers_domains: Mapped[list] = mapped_column(JSON, default=list)
    newsroom_domains: Mapped[list] = mapped_column(JSON, default=list)
    # Canonical ATS tenant URL prefixes for this competitor (scheme-stripped,
    # no trailing slash). Examples:
    #   "boards.greenhouse.io/adeccogroup"
    #   "jobs.lever.co/ashby"
    #   "adecco.myworkdayjobs.com/adecco_careers"
    # Used by the hiring sweep to scope job searches to this competitor's own
    # board on the ATS rather than the ATS root domain (which hosts every
    # customer's jobs). Populated by app/adapters/ats/discovery.py on first
    # scan when a careers page is available; operator can edit in admin UI.
    ats_tenants: Mapped[list] = mapped_column(JSON, default=list)
    # Apex domain (e.g. "linkedin.com") used to look up the company logo via
    # Apistemic and anywhere else we need a canonical company identifier. Kept
    # explicit rather than derived from careers/newsroom_domains because those
    # can legitimately point at a third-party host (e.g. boards.greenhouse.io).
    homepage_domain: Mapped[str | None] = mapped_column(String(128), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # Momentum tracking identifiers — all nullable, many competitors won't have apps.
    app_store_id: Mapped[str | None] = mapped_column(String(32), nullable=True)      # iOS App Store numeric ID, e.g. "284910350"
    play_package: Mapped[str | None] = mapped_column(String(128), nullable=True)     # Android package, e.g. "au.com.seek.jobs"
    trends_keyword: Mapped[str | None] = mapped_column(String(128), nullable=True)   # Override for Google Trends query (else uses name)
    # Per-competitor search-quality overrides. NULL = use global env defaults.
    # Useful when a big-brand competitor (LinkedIn, Indeed) has so much
    # unrelated noise that the default threshold still lets junk through.
    min_relevance_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    social_score_multiplier: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Positioning-extractor input override. Empty = auto-probe {homepage,
    # /pricing, /plans, /product, /features}. When set, replaces the
    # auto-probe list entirely (no merge).
    positioning_pages: Mapped[list] = mapped_column(JSON, default=list)


class CompetitorMetric(Base):
    """Daily time-series for competitor momentum signals (Google Trends interest,
    app store rank, install bucket, rating, etc.). One row per (competitor, metric, day).
    Each day's job UPSERTs — re-running the same day overwrites the row."""
    __tablename__ = "competitor_metrics"
    id: Mapped[int] = mapped_column(primary_key=True)
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitors.id"), index=True)
    metric: Mapped[str] = mapped_column(String(64), index=True)    # "google_trends", "ios_rank", "play_installs", "play_rating", "play_reviews"
    value: Mapped[float | None] = mapped_column(Float, nullable=True)   # null = signal unavailable today (rate-limited, not in top N, etc.)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)          # free-form context: {"country":"au", "keyword":"Seek", "category":"Business"}
    collected_date: Mapped[str] = mapped_column(String(10), index=True)  # "YYYY-MM-DD" — makes the unique-per-day constraint trivial
    collected_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))  # scan, discovery, prune, reply_check
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending, running, ok, error
    triggered_by: Mapped[str] = mapped_column(String(32), default="schedule")  # schedule, manual, api
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    findings_count: Mapped[int] = mapped_column(Integer, default=0)
    report_id: Mapped[int | None] = mapped_column(ForeignKey("reports.id"), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    events: Mapped[list["RunEvent"]] = relationship(back_populates="run", cascade="all, delete-orphan")


class RunEvent(Base):
    __tablename__ = "run_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    level: Mapped[str] = mapped_column(String(16), default="info")  # info, warn, error, material
    message: Mapped[str] = mapped_column(Text)
    # Optional structured context. For level="material" we stash
    # {competitor_id, competitor_name, signal_type, title, url} so the
    # live-log UI renders a badge + clickable link instead of raw text.
    meta: Mapped[dict] = mapped_column(JSON, default=dict)

    run: Mapped[Run] = relationship(back_populates="events")


class Finding(Base):
    __tablename__ = "findings"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"), index=True, nullable=True)
    competitor: Mapped[str] = mapped_column(String(255), index=True)
    source: Mapped[str] = mapped_column(String(64))
    topic: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    # LLM-written, display-ready snippet (≤320 chars). The stream card falls
    # back to `content` when this is NULL (legacy rows pre-backfill).
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    # Provenance + quality signals (added so we can compare provider performance)
    search_provider: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)  # "tavily", "serper", ...
    score: Mapped[float | None] = mapped_column(Float, nullable=True)                            # provider relevance, 0–1
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)              # when the source published the content
    # Signal-stream columns. Populated by the extraction layer; nullable so
    # legacy rows keep loading and can be backfilled lazily.
    # signal_type taxonomy: news | price_change | new_hire | product_launch |
    # messaging_shift | funding | integration | voc_mention | momentum_point | other
    signal_type: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)                                    # typed fields per signal_type
    materiality: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)          # 0.0–1.0, how worth surfacing
    # Which configured keyword produced this finding. Populated by the scanner
    # when a keyword-driven search returns the hit. NULL for findings from
    # non-keyword sources (careers sweep, ATS boards, customer sweep) or
    # legacy rows. Feeds the history-aware Optimise button: per-keyword
    # materiality distribution tells the tuning agent which terms earn
    # their keep and which should be pruned.
    matched_keyword: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    # Quality signal from the market-digest pass. The analyzer LLM labels
    # findings it features in the digest as HIGH / MEDIUM / LOW threat, or
    # flags them as NOISE when it explicitly calls them low-signal. NULL
    # means either (a) the finding predates digest-stamping, (b) the
    # digest didn't reference it at all (i.e. it was in the top-60 the
    # analyzer saw but didn't make the cut), or (c) the digest hasn't run
    # yet for this scan. This is a stronger "was this useful" proxy than
    # raw materiality, which only grades signal-type potential.
    digest_threat_level: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)


class Report(Base):
    __tablename__ = "reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(255))
    body_md: Mapped[str] = mapped_column(Text)
    file_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class ContextBrief(Base):
    """A synthesized brief for one of the 'context' entities — the company
    itself or its customers. Same shape as CompetitorReport but keyed by
    scope string instead of an FK. Append-only: current = latest row per
    scope. Regenerated manually or on scan completion."""
    __tablename__ = "context_briefs"
    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str] = mapped_column(String(32), index=True)  # "company" | "customer"
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    body_md: Mapped[str] = mapped_column(Text)
    source_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class CompetitorReport(Base):
    """A synthesized 'overall strategy review' for one competitor. Append-only:
    current = latest row for that competitor_id. Regenerated at the end of each
    market scan for active competitors with activity in the recency window."""
    __tablename__ = "competitor_reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitors.id"), index=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    body_md: Mapped[str] = mapped_column(Text)
    source_summary: Mapped[str | None] = mapped_column(Text, nullable=True)  # what was fed in
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class PositioningSnapshot(Base):
    """One extraction of a competitor's marketing-page positioning.
    Append-only: latest per competitor_id is the 'current' view, older rows
    are history surfaced as a collapsible list on the Positioning tab.

    Not tied to a scan Run — positioning extraction has its own monthly
    cadence and manual refresh button.
    """
    __tablename__ = "positioning_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitors.id"), index=True)
    # Structured pillars rendered as cards on the tab. Shape:
    #   [{"name": str, "weight": 1..5, "quote": str, "source_url": str}]
    pillars: Mapped[list] = mapped_column(JSON, default=list)
    # Narrative markdown with sections: Current positioning / What changed
    # since {date} / Evidence. Rendered via marked.js in the tab.
    # Empty string when the narrative LLM call failed but pillars succeeded.
    body_md: Mapped[str] = mapped_column(Text, default="")
    # URLs actually fetched that came back non-empty.
    source_urls: Mapped[list] = mapped_column(JSON, default=list)
    # SHA-256 of the concatenated fetched text. Matches prior snapshot =
    # pages unchanged = short-circuit, no LLM call, no new row.
    source_hash: Mapped[str] = mapped_column(String(64), index=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, index=True
    )

    __table_args__ = (
        Index(
            "ix_positioning_snapshots_competitor_created",
            "competitor_id",
            text("created_at DESC"),
        ),
    )


class DeepResearchReport(Base):
    """One Gemini Deep Research run for a competitor. Append-only:
    latest per competitor_id is the 'current' view, older rows are
    history surfaced as a collapsible list on the Research tab.

    Created in 'queued' state when the user clicks Run. The job flips
    to 'running' once Gemini confirms the interaction is active,
    'ready' when the final report is written, or 'failed' with the
    error message captured in `error`.
    """
    __tablename__ = "deep_research_reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitors.id"), index=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"), nullable=True, index=True)

    # Gemini-side identifier so we can resume polling after a server
    # restart. NULL until the adapter has created the interaction.
    interaction_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)

    # "preview" | "max" — frozen at creation, drives which model we pick.
    agent: Mapped[str] = mapped_column(String(32), default="preview")

    # "queued" | "running" | "ready" | "failed" | "cancelled"
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)

    # Filled-in skill template — persisted so the exact ask is auditable.
    brief: Mapped[str] = mapped_column(Text, default="")

    # Final markdown. Empty until status flips to 'ready'.
    body_md: Mapped[str] = mapped_column(Text, default="")

    # Normalized citations from the adapter. Shape:
    #   [{"title": str, "url": str, "published_at": str | None,
    #     "snippet": str | None}]
    sources: Mapped[list] = mapped_column(JSON, default=list)

    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, index=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        Index(
            "ix_deep_research_competitor_started",
            "competitor_id",
            text("started_at DESC"),
        ),
    )


class MarketSynthesisReport(Base):
    """One Gemini Deep Research run spanning every active competitor.
    Append-only: latest row is the 'current' synthesis shown on /market,
    older rows are history in a collapsible list. Unlike
    DeepResearchReport there is no competitor_id — one row covers the
    whole market.

    Created by the weekly cron (agent='max', triggered_by='scheduled')
    or by the manual Run button (agent='preview', triggered_by='manual').
    State machine mirrors DeepResearchReport so the resume-on-boot sweep
    and adapter stay shared.
    """
    __tablename__ = "market_synthesis_reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"), nullable=True, index=True)

    # Gemini-side identifier — keeps a row poll-resumable after a restart.
    interaction_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)

    # "preview" | "max". Frozen at creation; cron uses max, manual uses preview.
    agent: Mapped[str] = mapped_column(String(32), default="preview")

    # "queued" | "running" | "ready" | "failed" | "cancelled"
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)

    # "manual" | "scheduled" — mirrors Run.triggered_by so the row is
    # diagnosable without a Run join.
    triggered_by: Mapped[str] = mapped_column(String(16), default="manual")

    # Days of findings history that fed the brief. Stored so older syntheses
    # remain interpretable if we change the default later.
    window_days: Mapped[int] = mapped_column(Integer, default=30)

    # Filled-in skill template sent to Gemini — exact ask is auditable.
    brief: Mapped[str] = mapped_column(Text, default="")

    # Final markdown. Empty until status flips to 'ready'.
    body_md: Mapped[str] = mapped_column(Text, default="")

    # Same shape as DeepResearchReport.sources:
    #   [{"title": str, "url": str, "published_at": str | None,
    #     "snippet": str | None}]
    sources: Mapped[list] = mapped_column(JSON, default=list)

    # Composer telemetry — what the synthesis was actually built from.
    # Surfaced on the detail page so a thin read is diagnosable at a glance.
    # Shape: {"findings_count": int, "competitors_covered": int,
    #         "dr_reports_used": int, "brief_chars": int}
    inputs_meta: Mapped[dict] = mapped_column(JSON, default=dict)

    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, index=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)


class Skill(Base):
    __tablename__ = "skills"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    body_md: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class UsageEvent(Base):
    __tablename__ = "usage_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"), nullable=True, index=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)  # claude, tavily
    operation: Mapped[str | None] = mapped_column(String(64), nullable=True)  # messages.create, search
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    credits: Mapped[int] = mapped_column(Integer, default=0)  # tavily
    cost_usd: Mapped[float] = mapped_column(default=0.0)
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    bucket: Mapped[str] = mapped_column(String(32))  # seek, competitors
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    uploaded_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SignalView(Base):
    """Per-user read state for the stream. Insert-on-interact: absence of a
    row means 'new/unread', so writes only happen when the user actually
    acts on a signal."""
    __tablename__ = "signal_views"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    finding_id: Mapped[int] = mapped_column(ForeignKey("findings.id"), index=True)
    state: Mapped[str] = mapped_column(String(16))  # seen | pinned | dismissed | snoozed
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("user_id", "finding_id", name="uq_signal_views_user_finding"),
    )


class UserSignalEvent(Base):
    """Append-only log of per-user interactions with findings. Source of
    truth for the ranker's preference rollup (docs/ranker/01-signal-log.md).

    Separate from SignalView, which tracks current pin/dismiss/snooze state
    for the read-state UI. Pin/dismiss/snooze writes dual-write into both
    tables in the same transaction — this log is additive, SignalView is
    the materialized current state.

    Rows are immutable: never UPDATE, never DELETE except via the nightly
    retention prune (>180 days).
    """
    __tablename__ = "user_signal_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # Nullable for non-finding events (chat_pref_update). ON DELETE SET NULL
    # so removing a finding doesn't wipe the behavioural history the rollup
    # depends on.
    finding_id: Mapped[int | None] = mapped_column(
        ForeignKey("findings.id", ondelete="SET NULL"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # Raw event-specific magnitude (e.g. dwell_ms). Never a precomputed
    # weight — weight mapping lives in the rollup.
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        # Recent events for one user — rollup + debug UI. ts DESC so "latest
        # first" scans walk the index forward; matches the alembic migration.
        Index("ix_user_signal_events_user_ts", "user_id", text("ts DESC")),
        # Per-type slices for weight accounting in the rollup.
        Index(
            "ix_user_signal_events_user_type_ts",
            "user_id",
            "event_type",
            text("ts DESC"),
        ),
        # Reverse lookup for "which users reacted to this finding".
        Index("ix_user_signal_events_finding", "finding_id"),
    )


class UserPreferenceVector(Base):
    """Sparse per-user preference weights across structured dimensions.
    Rebuilt by the ranker's preference rollup (docs/ranker/02-preference-
    rollup.md); the ranker (spec 03) reads from here.

    Truncate-and-rewrite per user on each rollup — never incrementally
    updated. Dropping this table is safe: next rollup rebuilds it from
    user_signal_events.
    """
    __tablename__ = "user_preferences_vector"
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    dimension: Mapped[str] = mapped_column(String(32), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    # Ranker reads this directly. tanh-squashed raw_sum, always in [-1, +1].
    weight: Mapped[float] = mapped_column(Float, nullable=False)
    # Unsquashed decayed sum. Kept so the tanh squash can be retuned later
    # without rereading the event log.
    raw_sum: Mapped[float] = mapped_column(Float, nullable=False)
    evidence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    positive_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    negative_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_event_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        Index("ix_user_preferences_vector_user_dim", "user_id", "dimension"),
        # "top interests" queries — top N by weight per user. DESC so the
        # common case ("strongest preferences first") walks the index
        # forward; matches the alembic migration.
        Index("ix_user_preferences_vector_user_weight", "user_id", text("weight DESC")),
    )


class UserPreferenceProfile(Base):
    """One row per user. Holds the LLM-editable taste_doc (written by
    spec 04, untouched by the rollup) plus rollup metadata. Separate from
    user_preferences_vector because the rollup truncates the vector on
    every run — we don't want to also blow away the taste doc.
    """
    __tablename__ = "user_preference_profile"
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    taste_doc: Mapped[str | None] = mapped_column(Text, nullable=True)
    cold_start: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    event_count_30d: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_computed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Bumped when the weight mapping changes in ways that invalidate
    # cached vectors (config.py). Rollup compares stored vs. current and
    # forces a rebuild when they differ.
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class SavedFilter(Base):
    """Named stream view. owner_id NULL = team-shared."""
    __tablename__ = "saved_filters"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    spec: Mapped[dict] = mapped_column(JSON, default=dict)  # {signal_types, competitor_ids, min_materiality, since_days, sources}
    visibility: Mapped[str] = mapped_column(String(16), default="private")  # private | team
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
