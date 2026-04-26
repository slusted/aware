"""VoC theme synthesis pass.

For one competitor, take the recent app-review corpus + current themes
and ask Haiku to produce an updated theme set with diff classifications.
Persists themes in place; emits `Finding(signal_type="voc_theme")` only
on emergence or material shift.

Themes are rolling state, not events. We update existing rows rather than
inserting new ones for the same theme — labels are stable across runs.
The diff classification ('new' / 'same' / 'shifted' / 'dropped') is what
decides whether a finding emits.

Spec: docs/voc/01-app-reviews.md
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import anthropic
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import AppReview, AppReviewSource, Competitor, Finding, ReviewTheme
from .skills import load_active


MODEL = "claude-haiku-4-5-20251001"

_DEFAULT_MAX_REVIEWS = 200
_DEFAULT_MAX_THEMES = 8
_DEFAULT_SHIFT_THRESHOLD_PCT = 50
_MIN_REVIEWS_FOR_SYNTHESIS = 10  # spec: skip the long tail
_REVIEW_BODY_CAP = 600           # truncate per review to keep prompt size bounded
_LABEL_CAP = 250
_DESCRIPTION_CAP = 600

_VALID_SENTIMENTS = {"positive", "negative", "mixed"}
_VALID_DIFF_KINDS = {"new", "same", "shifted", "dropped"}


_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic | None:
    global _client
    if _client is not None:
        return _client
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    _client = anthropic.Anthropic()
    return _client


@dataclass
class CompetitorSynthesisResult:
    competitor_id: int
    competitor_name: str
    themes_total: int = 0
    new: int = 0
    shifted: int = 0
    dropped: int = 0
    findings_emitted: int = 0
    skipped_reason: str | None = None  # set when synthesis didn't run for this competitor


@dataclass
class SweepResult:
    competitors_processed: int = 0
    competitors_skipped: int = 0
    findings_emitted: int = 0
    per_competitor: list[CompetitorSynthesisResult] = field(default_factory=list)


_FALLBACK_PROMPT = """\
You are a customer-research analyst summarising app-store reviews for {{competitor_name}}.

You will receive recent reviews and the current set of themes (may be empty).

Produce up to 8 themes. A theme is a short noun phrase (≤80 chars) describing
one specific issue, delight, or pattern. For each theme: label, description,
sentiment ("positive"|"negative"|"mixed"), volume_30d, volume_prev_30d,
sample_review_ids (3–5).

For each theme also classify how it changed vs the current themes input:
"new" | "same" | "shifted" | "dropped". Renaming themes between runs makes
trends unreadable — keep the prior label verbatim when the theme is still
present. Don't invent themes from 1–2 reviews.

Current themes:
{{current_themes_json}}

Recent reviews:
{{reviews_json}}

Respond with ONLY JSON:
{
  "themes": [
    {"label":"...","description":"...","sentiment":"...","volume_30d":0,
     "volume_prev_30d":0,"sample_review_ids":["..."]}
  ],
  "diff": [
    {"label":"...","kind":"new|same|shifted|dropped","current_theme_id":null}
  ]
}
"""


def _render_prompt(competitor_name: str, current_themes: list[dict], reviews: list[dict]) -> str:
    template = load_active("voc_theme_synthesise") or _FALLBACK_PROMPT
    vals = {
        "competitor_name":     competitor_name or "the competitor",
        "current_themes_json": json.dumps(current_themes, ensure_ascii=False, indent=2),
        "reviews_json":        json.dumps(reviews, ensure_ascii=False, indent=2),
    }
    out = template
    for k, v in vals.items():
        out = out.replace("{{" + k + "}}", v)
    return out


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> dict | None:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    m = _JSON_BLOCK_RE.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def _config(config: dict | None) -> dict:
    block = (config or {}).get("app_reviews") or {}
    return {
        "max_reviews":  int(block.get("synthesise_max_reviews", _DEFAULT_MAX_REVIEWS)),
        "max_themes":   int(block.get("max_themes_per_competitor", _DEFAULT_MAX_THEMES)),
        "shift_pct":    int(block.get("shift_threshold_pct", _DEFAULT_SHIFT_THRESHOLD_PCT)),
    }


def _load_recent_reviews(db: Session, competitor_id: int, limit: int) -> list[AppReview]:
    return (
        db.query(AppReview)
        .filter(AppReview.competitor_id == competitor_id)
        .order_by(AppReview.posted_at.desc().nullslast(), AppReview.ingested_at.desc())
        .limit(limit)
        .all()
    )


def _competitor_review_count_60d(db: Session, competitor_id: int) -> int:
    cutoff = datetime.utcnow() - timedelta(days=60)
    return (
        db.query(AppReview)
        .filter(
            AppReview.competitor_id == competitor_id,
            AppReview.ingested_at >= cutoff,
        )
        .count()
    )


def _serialize_reviews(reviews: list[AppReview]) -> list[dict]:
    out = []
    for r in reviews:
        body = (r.body or "").strip()
        if len(body) > _REVIEW_BODY_CAP:
            body = body[:_REVIEW_BODY_CAP - 1] + "…"
        out.append({
            "id": str(r.id),
            "rating": r.rating,
            "posted_at": r.posted_at.isoformat() if r.posted_at else None,
            "title": r.title,
            "body": body,
        })
    return out


def _serialize_current_themes(themes: list[ReviewTheme]) -> list[dict]:
    return [
        {
            "id": t.id,
            "label": t.label,
            "description": t.description,
            "sentiment": t.sentiment,
            "volume_30d": t.volume_30d,
        }
        for t in themes
        if t.status == "active"
    ]


def _theme_finding_hash(competitor_id: int, theme_id: int, kind: str, run_id: int | None) -> str:
    """Deterministic hash so re-runs of the same Run don't double-insert,
    but a different Run (next week's synthesis) gets its own row."""
    seed = f"voc_theme|{competitor_id}|{theme_id}|{kind}|{run_id or 0}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _sentiment_flipped(prior: str, current: str) -> bool:
    """A polarity flip is positive↔negative. mixed→either or either→mixed
    is movement but not a flip — treated as not material on its own."""
    if prior not in _VALID_SENTIMENTS or current not in _VALID_SENTIMENTS:
        return False
    return (prior == "positive" and current == "negative") or (
        prior == "negative" and current == "positive"
    )


def _volume_shifted(prior: int, current: int, threshold_pct: int) -> bool:
    if prior <= 0 and current <= 0:
        return False
    if prior <= 0:
        # Going from 0 to anything >= 5 is itself a material shift.
        return current >= 5
    delta_pct = abs(current - prior) * 100 / prior
    return delta_pct >= threshold_pct


def _emit_finding(
    db: Session,
    competitor_name: str,
    competitor_id: int,
    theme: ReviewTheme,
    kind: str,
    run_id: int | None,
) -> bool:
    """Insert a Finding row for theme emergence / shift. Returns True on
    insert, False if the hash collided (idempotent re-run)."""
    if kind == "emerged":
        title = f"New theme: {theme.label}"
        summary = f"New theme detected in {competitor_name} app reviews — {theme.label}."
    else:  # "shifted"
        delta = theme.volume_30d - theme.volume_prev_30d
        arrow = "▲" if delta >= 0 else "▼"
        title = f"Theme {arrow} {theme.label}"
        summary = (
            f"Theme '{theme.label}' shifted in {competitor_name} app reviews — "
            f"{theme.volume_prev_30d} → {theme.volume_30d} reviews "
            f"(sentiment {theme.sentiment})."
        )

    payload = {
        "kind": kind,
        "theme_id": theme.id,
        "label": theme.label,
        "sentiment": theme.sentiment,
        "volume_30d": theme.volume_30d,
        "volume_prev_30d": theme.volume_prev_30d,
        "sample_review_ids": list(theme.sample_review_ids or []),
    }
    h = _theme_finding_hash(competitor_id, theme.id, kind, run_id)
    finding = Finding(
        run_id=run_id,
        competitor=competitor_name,
        source="app_store",
        topic="voice of customer",
        title=title,
        summary=summary,
        content=theme.description or summary,
        url=None,
        hash=h,
        signal_type="voc_theme",
        payload=payload,
        materiality=0.7 if kind == "emerged" else 0.6,
    )
    db.add(finding)
    try:
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False


def _coerce_label(raw) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if len(s) > _LABEL_CAP:
        s = s[:_LABEL_CAP].rstrip()
    return s


def _coerce_description(raw) -> str:
    if raw is None:
        return ""
    s = str(raw).strip()
    if len(s) > _DESCRIPTION_CAP:
        s = s[:_DESCRIPTION_CAP].rstrip() + "…"
    return s


def _coerce_int(raw, default: int = 0) -> int:
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return default


def _coerce_sentiment(raw) -> str:
    s = str(raw or "").strip().lower()
    return s if s in _VALID_SENTIMENTS else "mixed"


def _coerce_sample_ids(raw) -> list[str]:
    if not isinstance(raw, list):
        return []
    out = []
    for v in raw:
        try:
            sid = str(v).strip()
        except Exception:
            continue
        if sid and sid not in out:
            out.append(sid)
        if len(out) >= 10:
            break
    return out


def synthesise_for_competitor(
    db: Session,
    competitor: Competitor,
    config: dict | None = None,
    run_id: int | None = None,
    force: bool = False,
) -> CompetitorSynthesisResult:
    """One competitor: load reviews, call Haiku, reconcile themes, emit findings."""
    cfg = _config(config)
    res = CompetitorSynthesisResult(competitor_id=competitor.id, competitor_name=competitor.name)

    # Skip the long tail unless caller forces (manual admin trigger).
    if not force and _competitor_review_count_60d(db, competitor.id) < _MIN_REVIEWS_FOR_SYNTHESIS:
        res.skipped_reason = "fewer than 10 reviews in last 60 days"
        return res

    reviews = _load_recent_reviews(db, competitor.id, cfg["max_reviews"])
    if not reviews:
        res.skipped_reason = "no reviews ingested yet"
        return res

    current_themes = (
        db.query(ReviewTheme)
        .filter(ReviewTheme.competitor_id == competitor.id)
        .all()
    )
    active_current = [t for t in current_themes if t.status == "active"]

    client = _get_client()
    if client is None:
        res.skipped_reason = "ANTHROPIC_API_KEY not set"
        return res

    prompt = _render_prompt(
        competitor.name,
        _serialize_current_themes(active_current),
        _serialize_reviews(reviews),
    )

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text
    except Exception as e:
        res.skipped_reason = f"LLM call failed: {e}"
        return res

    parsed = _extract_json(raw)
    if not parsed or not isinstance(parsed, dict):
        res.skipped_reason = "LLM response not parseable as JSON"
        return res

    themes_out = parsed.get("themes") or []
    diff_out = parsed.get("diff") or []
    if not isinstance(themes_out, list) or not isinstance(diff_out, list):
        res.skipped_reason = "LLM response missing themes/diff arrays"
        return res

    # Build a label → diff_kind map from the diff array.
    diff_by_label: dict[str, str] = {}
    for d in diff_out:
        if not isinstance(d, dict):
            continue
        label = _coerce_label(d.get("label"))
        kind = str(d.get("kind") or "").strip().lower()
        if label and kind in _VALID_DIFF_KINDS:
            diff_by_label[label] = kind

    # Index current themes by label for cheap lookup.
    current_by_label: dict[str, ReviewTheme] = {t.label: t for t in current_themes}

    # Cap themes at config max.
    themes_out = themes_out[: cfg["max_themes"]]

    review_ids_in_corpus: set[str] = {str(r.id) for r in reviews}
    findings_emitted = 0
    seen_theme_ids: set[int] = set()

    for raw_theme in themes_out:
        if not isinstance(raw_theme, dict):
            continue
        label = _coerce_label(raw_theme.get("label"))
        if not label:
            continue

        diff_kind = diff_by_label.get(label, "new")
        sentiment = _coerce_sentiment(raw_theme.get("sentiment"))
        description = _coerce_description(raw_theme.get("description"))
        volume_30d = _coerce_int(raw_theme.get("volume_30d"))
        volume_prev_30d = _coerce_int(raw_theme.get("volume_prev_30d"))
        sample_ids = [
            sid for sid in _coerce_sample_ids(raw_theme.get("sample_review_ids"))
            if sid in review_ids_in_corpus
        ]

        existing = current_by_label.get(label)

        if existing is None:
            # New theme.
            theme = ReviewTheme(
                competitor_id=competitor.id,
                label=label,
                description=description,
                sentiment=sentiment,
                volume_30d=volume_30d,
                volume_prev_30d=volume_prev_30d,
                sample_review_ids=sample_ids,
                first_seen=datetime.utcnow(),
                last_seen=datetime.utcnow(),
                status="active",
                last_run_id=run_id,
            )
            db.add(theme)
            db.commit()
            db.refresh(theme)
            seen_theme_ids.add(theme.id)
            res.new += 1
            if _emit_finding(db, competitor.name, competitor.id, theme, "emerged", run_id):
                findings_emitted += 1
            res.themes_total += 1
        else:
            # Existing theme — update in place.
            prior_volume = existing.volume_30d
            prior_sentiment = existing.sentiment
            existing.description = description
            existing.sentiment = sentiment
            existing.volume_prev_30d = volume_prev_30d if diff_kind != "same" else prior_volume
            existing.volume_30d = volume_30d
            existing.sample_review_ids = sample_ids
            existing.last_seen = datetime.utcnow()
            existing.status = "active"
            existing.last_run_id = run_id
            db.commit()
            seen_theme_ids.add(existing.id)
            res.themes_total += 1

            is_shift = (
                diff_kind == "shifted"
                and (
                    _volume_shifted(prior_volume, volume_30d, cfg["shift_pct"])
                    or _sentiment_flipped(prior_sentiment, sentiment)
                )
            )
            if is_shift:
                res.shifted += 1
                if _emit_finding(db, competitor.name, competitor.id, existing, "shifted", run_id):
                    findings_emitted += 1

    # Anything in current_themes not seen this run → mark dormant.
    for t in current_themes:
        if t.id in seen_theme_ids:
            continue
        if t.status == "active":
            t.status = "dormant"
            t.last_run_id = run_id
            res.dropped += 1
    db.commit()

    # Backfill app_reviews.theme_id for the samples we kept. Each sample
    # id is the review's PK as a string. We do this after persistence so
    # all theme rows have ids assigned.
    active_themes = (
        db.query(ReviewTheme)
        .filter(
            ReviewTheme.competitor_id == competitor.id,
            ReviewTheme.status == "active",
            ReviewTheme.id.in_(seen_theme_ids) if seen_theme_ids else False,
        )
        .all()
    )
    for theme in active_themes:
        for sid_str in (theme.sample_review_ids or []):
            try:
                sid = int(sid_str)
            except (TypeError, ValueError):
                continue
            db.query(AppReview).filter(AppReview.id == sid).update(
                {"theme_id": theme.id}
            )
    db.commit()

    res.findings_emitted = findings_emitted
    return res


def synthesise_all(
    db: Session,
    config: dict | None = None,
    run_id: int | None = None,
) -> SweepResult:
    """Sweep every active competitor that has at least one enabled source."""
    sweep = SweepResult()
    eligible = (
        db.query(Competitor)
        .join(AppReviewSource, AppReviewSource.competitor_id == Competitor.id)
        .filter(
            Competitor.active == True,             # noqa: E712
            AppReviewSource.enabled == True,        # noqa: E712
        )
        .distinct()
        .all()
    )
    for comp in eligible:
        per = synthesise_for_competitor(db, comp, config=config, run_id=run_id)
        sweep.per_competitor.append(per)
        if per.skipped_reason:
            sweep.competitors_skipped += 1
        else:
            sweep.competitors_processed += 1
        sweep.findings_emitted += per.findings_emitted
    return sweep
