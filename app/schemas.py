from datetime import datetime
from pydantic import BaseModel, ConfigDict


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class CompetitorOut(ORM):
    id: int
    name: str
    category: str | None
    source: str
    threat_angle: str | None
    keywords: list[str] = []
    subreddits: list[str] = []
    careers_domains: list[str] = []
    newsroom_domains: list[str] = []
    homepage_domain: str | None = None
    active: bool
    last_activity_at: datetime | None
    app_store_id: str | None = None
    play_package: str | None = None
    trends_keyword: str | None = None
    min_relevance_score: float | None = None
    social_score_multiplier: float | None = None
    positioning_pages: list[str] = []


class CompetitorIn(BaseModel):
    name: str
    category: str | None = None
    threat_angle: str | None = None
    keywords: list[str] = []
    subreddits: list[str] = []
    careers_domains: list[str] = []
    newsroom_domains: list[str] = []
    homepage_domain: str | None = None
    app_store_id: str | None = None
    play_package: str | None = None
    trends_keyword: str | None = None
    min_relevance_score: float | None = None
    social_score_multiplier: float | None = None
    positioning_pages: list[str] = []


class RunOut(ORM):
    id: int
    kind: str
    status: str
    triggered_by: str
    started_at: datetime
    finished_at: datetime | None
    findings_count: int
    error: str | None


class RunEventOut(ORM):
    id: int
    ts: datetime
    level: str
    message: str
    meta: dict = {}


class FindingOut(ORM):
    id: int
    competitor: str
    source: str
    topic: str | None
    title: str | None
    url: str | None
    content: str | None
    summary: str | None = None
    created_at: datetime
    # Signal-stream fields (nullable for legacy rows not yet classified)
    signal_type: str | None = None
    materiality: float | None = None
    published_at: datetime | None = None
    search_provider: str | None = None
    score: float | None = None
    # Per-user view state (joined from SignalView at query time).
    # view_state is None when the viewer has never interacted with this signal.
    view_state: str | None = None
    snoozed_until: datetime | None = None


class SignalViewIn(BaseModel):
    state: str  # seen | pinned | dismissed | snoozed
    snoozed_until: datetime | None = None


class SignalEventIn(BaseModel):
    """One user-signal event submission. Taxonomy validation happens in the
    route via app.ranker.events.validate_event — keeping the pydantic shape
    permissive lets us return a clean 400 with the exact reason instead of
    a noisy pydantic error envelope."""
    event_type: str
    source: str
    finding_id: int | None = None
    value: float | None = None
    meta: dict = {}


class SignalEventBatchIn(BaseModel):
    events: list[SignalEventIn]


class SavedFilterOut(ORM):
    id: int
    owner_id: int | None
    name: str
    spec: dict
    visibility: str
    created_at: datetime


class SavedFilterIn(BaseModel):
    name: str
    spec: dict = {}
    visibility: str = "private"  # private | team


class ReportOut(ORM):
    id: int
    title: str
    created_at: datetime


class ReportDetail(ReportOut):
    body_md: str


class StatusOut(BaseModel):
    last_run: RunOut | None
    next_run_at: datetime | None
    is_running: bool
    competitor_count: int
    findings_today: int
