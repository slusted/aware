"""Thin wrappers around the existing scanner/analyzer/service modules.
Each job writes Run + RunEvent + Finding/Report rows so the UI has visibility.
The heavy lifting stays in the original modules — this layer just logs to DB.
"""
import contextlib
import os
import sys
import threading
import traceback
from datetime import datetime, timedelta

from .db import SessionLocal
from .models import Run, RunEvent, Finding, Report, UserSignalEvent, DeepResearchReport, MarketSynthesisReport, Competitor, CompetitorCandidate
from .usage import current_run_id


# ── Run cancellation ─────────────────────────────────────────────
# Best-effort cooperative cancellation. The HTTP endpoint adds the run_id
# to `_cancelled` and flips status to "cancelling"; the job polls
# `check_cancel` at natural boundaries (between competitors, before review
# synthesis, etc.) and raises RunCancelled, which the wrapper catches to
# finish the run with status="cancelled". We don't attempt to interrupt
# blocking HTTP calls mid-flight — the current network request completes,
# but no new work is scheduled.
class RunCancelled(Exception):
    pass


_cancel_lock = threading.Lock()
_cancelled: set[int] = set()


def request_cancel(run_id: int) -> None:
    with _cancel_lock:
        _cancelled.add(run_id)


def is_cancelled(run_id: int) -> bool:
    with _cancel_lock:
        return run_id in _cancelled


def clear_cancel(run_id: int) -> None:
    with _cancel_lock:
        _cancelled.discard(run_id)


def check_cancel(run_id: int) -> None:
    if is_cancelled(run_id):
        raise RunCancelled(f"run {run_id} cancelled")


class _StreamToRunEvents:
    """File-like sink: each newline-terminated write becomes a RunEvent.
    Wrap scanner/analyzer calls with contextlib.redirect_stdout(this) so their
    existing print() calls become live-tailable events in the UI.

    Thread-safe line assembly: each thread accumulates into its own buffer
    until it sees a newline, then emits complete lines atomically under the
    shared lock. Without this, print() from N worker threads interleaves
    (print emits the message and the '\\n' as separate writes) and you get
    concatenated lines like '[scan] Adecco...[scan] Google Jobs...'.
    """

    def __init__(self, run_id: int):
        self.run_id = run_id
        self._tls = threading.local()            # per-thread write buffer
        self._lock = threading.Lock()            # guards _db + real stdout
        self._real = sys.__stdout__
        self._db = SessionLocal()

    def _emit_lines(self, lines: list[str]):
        """Under the shared lock: echo to terminal + persist to DB."""
        with self._lock:
            try:
                for line in lines:
                    self._real.write(line + "\n")
            except Exception:
                pass
            try:
                for line in lines:
                    stripped = line.strip()
                    if stripped:
                        self._db.add(RunEvent(
                            run_id=self.run_id, level="info", message=stripped,
                        ))
                self._db.commit()
            except Exception:
                self._db.rollback()

    def write(self, s: str):
        # Thread-local buffer — collect into full lines before touching shared state.
        buf = getattr(self._tls, "buf", "")
        buf += s
        if "\n" not in buf:
            self._tls.buf = buf
            return
        lines = []
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            lines.append(line)
        self._tls.buf = buf  # keep any trailing partial line
        self._emit_lines(lines)

    def flush(self):
        # Flush this thread's partial line (if any) as one last full line.
        buf = getattr(self._tls, "buf", "")
        if buf:
            self._tls.buf = ""
            self._emit_lines([buf])
        try:
            with self._lock:
                self._real.flush()
        except Exception:
            pass

    def close(self):
        try:
            self.flush()
        finally:
            try:
                self._db.close()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# Findings with materiality at or above this threshold get a dedicated
# RunEvent (level="material") so they surface in the live log as a badge
# with a link to the competitor page — not just as a "N new items" counter.
# 0.6 catches funding / new_hire / product_launch / price_change /
# integration; filters out the generic "news" (0.3) and voc_mention (0.4)
# buckets that would otherwise flood the stream.
MATERIAL_EVENT_THRESHOLD = 0.6


def _emit_material_event(db, run_id: int, finding: dict,
                         competitor_id: int | None,
                         signal_type: str, materiality: float | None) -> None:
    """If this finding crosses the materiality threshold, write a RunEvent
    the live log can render as a link to the competitor page. Safe to call
    for every finding — no-op below threshold or when we can't resolve a
    competitor_id (no link target = not useful in the stream)."""
    if materiality is None or materiality < MATERIAL_EVENT_THRESHOLD:
        return
    if not competitor_id:
        return
    name = finding.get("competitor") or ""
    title = (finding.get("title") or finding.get("content") or "").strip()
    if len(title) > 200:
        title = title[:197] + "..."
    msg = f"[material] {name} · {signal_type} · \"{title}\""
    db.add(RunEvent(
        run_id=run_id,
        level="material",
        message=msg,
        meta={
            "competitor_id": competitor_id,
            "competitor_name": name,
            "signal_type": signal_type,
            "materiality": materiality,
            "title": title,
            "url": finding.get("url"),
        },
    ))


def _stamp_digest_threat_levels(db, run_id: int, features: list[dict]) -> int:
    """Apply the analyzer's per-finding threat labels to the Finding rows
    for this run. Returns the count of rows updated.

    Match strategy: scope to findings from this run_id, then look up by
    (competitor, title) — the analyzer quotes titles verbatim from the
    input. Fallback: match by URL within the run if title matching misses
    (sometimes the LLM trims whitespace or punctuation off the title).
    We don't fuzzy-match beyond that — a missed stamp is preferable to a
    wrong one, and the keyword report treats 'not stamped' as its own
    signal (means the finding wasn't featured even as NOISE).

    No commit here — caller owns transaction boundary."""
    if not features:
        return 0
    updated = 0
    # Pull all findings for this run once; matching in Python is cheaper
    # than N round trips and the row count is bounded (60 cap upstream).
    candidates = db.query(Finding).filter(Finding.run_id == run_id).all()
    by_title: dict[tuple[str, str], Finding] = {}
    by_url: dict[str, Finding] = {}
    for f in candidates:
        if f.title:
            by_title[(f.competitor or "", f.title.strip())] = f
        if f.url:
            by_url[f.url.strip()] = f
    for item in features:
        level = item.get("threat_level")
        if level not in ("HIGH", "MEDIUM", "LOW", "NOISE"):
            continue
        row: Finding | None = None
        comp = (item.get("competitor") or "").strip()
        title = (item.get("title") or "").strip()
        url = (item.get("url") or "").strip()
        if comp and title:
            row = by_title.get((comp, title))
        if row is None and title:
            # Title-only fallback — competitor may be missing or renamed.
            for (_c, t), candidate in by_title.items():
                if t == title:
                    row = candidate
                    break
        if row is None and url:
            row = by_url.get(url)
        if row is not None:
            row.digest_threat_level = level
            updated += 1
    return updated


def _coerce_score(val) -> float | None:
    """Coerce a finding's relevance value to a float for the Finding.score column.
    Upstream sometimes boosts scores (e.g. +0.2 for official sources) so values
    can exceed 1.0; we accept that. Returns None if the value isn't numeric."""
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _parse_published(val) -> datetime | None:
    """Parse the 'published' string from a search result into a datetime.
    Handles:
      - ISO 8601 ('2026-04-10T14:32:00Z') — Tavily
      - Common date formats ('Apr 10, 2026')
      - Fuzzy 'X days ago' / 'X months ago' — Serper / Google News
    """
    if not val or not isinstance(val, str):
        return None
    s = val.strip()
    if not s:
        return None

    # ISO 8601 with or without trailing Z
    try:
        s2 = s[:-1] + "+00:00" if s.endswith("Z") else s
        return datetime.fromisoformat(s2).replace(tzinfo=None)
    except ValueError:
        pass

    # Common alternate formats that occasionally appear
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d %b %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue

    # Fuzzy '3 days ago', '5 months ago', '1 year ago' (Serper / Google News)
    import re
    from datetime import timedelta
    m = re.match(
        r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago",
        s, re.IGNORECASE,
    )
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        unit_map = {
            "second": timedelta(seconds=n),
            "minute": timedelta(minutes=n),
            "hour":   timedelta(hours=n),
            "day":    timedelta(days=n),
            "week":   timedelta(weeks=n),
            "month":  timedelta(days=n * 30),
            "year":   timedelta(days=n * 365),
        }
        delta = unit_map.get(unit)
        if delta is not None:
            return datetime.utcnow() - delta

    return None


def _log(db, run: Run, msg: str, level: str = "info"):
    """Coarse-grained event from the job wrapper itself. Writes directly to the
    terminal (bypassing any active redirect) so we don't double-log when the
    stdout tee is active."""
    db.add(RunEvent(run_id=run.id, level=level, message=msg))
    db.commit()
    try:
        sys.__stdout__.write(f"  [run#{run.id}] {msg}\n")
        sys.__stdout__.flush()
    except Exception:
        pass


def _start_run(
    kind: str,
    triggered_by: str = "schedule",
    run_id: int | None = None,
) -> tuple[Run, object]:
    """Either create a new running Run, or attach to an existing row that the
    drainer already flipped to 'running'. Cron / direct callers pass no
    run_id and get the legacy create-and-run path. Queue-dispatched callers
    pass the row's id so we don't insert a duplicate."""
    db = SessionLocal()
    if run_id is not None:
        run = db.get(Run, run_id)
        if run is None:
            # Defensive: drainer should never call us with a missing id, but
            # falling back to a fresh row is safer than a NoneType crash.
            run = Run(kind=kind, status="running", triggered_by=triggered_by)
            db.add(run)
            db.commit()
            db.refresh(run)
    else:
        run = Run(kind=kind, status="running", triggered_by=triggered_by)
        db.add(run)
        db.commit()
        db.refresh(run)
    return run, db


def _finish_run(db, run: Run, status: str = "ok", error: str | None = None):
    run.status = status
    run.finished_at = datetime.utcnow()
    run.error = error
    db.commit()
    db.close()


# ── Run queue ────────────────────────────────────────────────────
# Single-slot DB-backed queue. Trigger endpoints insert a row with
# status='queued'; the drainer (registered by app/scheduler.py) wakes
# every few seconds, picks the oldest queued row when nothing is
# running, flips it to 'running', and dispatches the matching job
# function on a worker thread. See docs/runs/01-run-queue.md.

# Soft cap on queued+running rows; trigger endpoints return 429 above it.
RUN_QUEUE_MAX = int(os.environ.get("RUN_QUEUE_MAX", "10"))


def queue_depth(db) -> int:
    """Number of currently-queued runs (waiting + about-to-run). Excludes
    the running/cancelling row itself."""
    return (
        db.query(Run)
        .filter(Run.status == "queued")
        .count()
    )


def queue_position(db, run_id: int) -> int:
    """1-based FIFO position of a queued run. Returns 0 if the run isn't
    queued (e.g. already picked up). Cheap: one indexed count."""
    row = db.get(Run, run_id)
    if row is None or row.status != "queued":
        return 0
    return (
        db.query(Run)
        .filter(Run.status == "queued", Run.id <= run_id)
        .count()
    )


def is_anything_in_flight(db) -> bool:
    return (
        db.query(Run)
        .filter(Run.status.in_(["running", "cancelling"]))
        .first()
        is not None
    )


def enqueue_run(
    db,
    kind: str,
    *,
    triggered_by: str = "manual",
    job_args: dict | None = None,
) -> Run:
    """Insert a queued Run row + an initial RunEvent. Returns the row.
    Caller is responsible for the queue-cap check (so the endpoint can
    return 429 before this is invoked)."""
    run = Run(
        kind=kind,
        status="queued",
        triggered_by=triggered_by,
        job_args=dict(job_args or {}),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    db.add(
        RunEvent(
            run_id=run.id,
            level="info",
            message=f"queued ({kind})"
            + (f" args={dict(job_args)}" if job_args else ""),
        )
    )
    db.commit()
    return run


def _dispatch_queued_run(run_id: int, kind: str, args: dict, triggered_by: str) -> None:
    """Worker-thread entrypoint. The drainer has already flipped the row to
    'running' and committed; we just call the matching job function with
    `run_id=` so _start_run reuses the row. Errors flip the row to 'error'
    via the job's own try/finally — but if dispatch itself fails (unknown
    kind, import error), we mark the row error here so it doesn't sit
    'running' forever."""
    try:
        if kind == "scan":
            run_scan_job(triggered_by, args.get("days"), run_id=run_id)
        elif kind == "discovery":
            run_discovery_job(run_id=run_id)
        elif kind == "market_digest":
            run_market_digest_job(triggered_by, run_id=run_id)
        elif kind == "ingest_app_reviews":
            run_ingest_app_reviews_job(triggered_by, run_id=run_id)
        elif kind == "synthesise_voc_themes":
            run_voc_themes_job(
                competitor_id=args.get("competitor_id"),
                triggered_by=triggered_by,
                force=bool(args.get("force", False)),
                run_id=run_id,
            )
        else:
            raise ValueError(f"queue dispatch: unknown run kind {kind!r}")
    except Exception as e:
        # Job functions own their own error path; this only fires if the
        # dispatch itself blew up before _start_run took over.
        tb = traceback.format_exc()
        db = SessionLocal()
        try:
            row = db.get(Run, run_id)
            if row and row.status in ("queued", "running", "cancelling"):
                row.status = "error"
                row.error = f"dispatch failed: {e}"
                row.finished_at = datetime.utcnow()
                db.add(
                    RunEvent(
                        run_id=run_id,
                        level="error",
                        message=f"dispatch failed: {e}\n{tb}",
                    )
                )
                db.commit()
        finally:
            db.close()


def drain_run_queue() -> None:
    """APScheduler entrypoint. Runs every few seconds. If nothing's running,
    picks the oldest queued row, flips it to 'running', and hands it off to
    a worker thread. max_instances=1 on the scheduler job ensures only one
    drainer tick is in flight at a time."""
    db = SessionLocal()
    try:
        if is_anything_in_flight(db):
            return
        nxt = (
            db.query(Run)
            .filter(Run.status == "queued")
            .order_by(Run.id.asc())
            .first()
        )
        if not nxt:
            return
        nxt.status = "running"
        db.add(
            RunEvent(
                run_id=nxt.id,
                level="info",
                message="drainer picked up",
            )
        )
        db.commit()
        kind = nxt.kind
        args = dict(nxt.job_args or {})
        triggered_by = nxt.triggered_by
        run_id = nxt.id
    finally:
        db.close()

    threading.Thread(
        target=_dispatch_queued_run,
        args=(run_id, kind, args, triggered_by),
        daemon=True,
        name=f"run-{run_id}-{kind}",
    ).start()


def _compute_freshness(db, explicit: int | None) -> int:
    """Days-of-web-content to include in this scan. Explicit override wins;
    otherwise default is 'since last successful scan' — capped to a sensible
    range so we don't burn credits on a huge crawl when the gap is unusual."""
    if explicit is not None:
        return max(1, min(int(explicit), 365))
    last_ok = (
        db.query(Run)
        .filter(Run.kind == "scan", Run.status == "ok")
        .order_by(Run.started_at.desc())
        .first()
    )
    if not last_ok:
        return 7  # first-ever scan: reasonable seed window
    gap_days = (datetime.utcnow() - last_ok.started_at).days
    return max(1, min(gap_days + 1, 30))  # +1 buffer, cap at 30


def _ensure_ats_tenants_discovered(comp_dict: dict) -> None:
    """If this competitor has careers_domains but no ats_tenants yet,
    crawl the careers page, regex out canonical ATS tenant prefixes
    (Greenhouse/Lever/Ashby/Workday/etc.), persist them, and inject the
    list back into `comp_dict` so the scanner that runs next sees them.

    Mutates `comp_dict['ats_tenants']` in place. Silent on misses —
    discovery failure is normal for competitors who self-host their
    careers page with no ATS embed, and the scanner's hiring sweep
    still works using careers_domains scope alone. No exception
    propagation: the caller wraps this in a try/except so one bad
    page never blocks the scan.
    """
    name = comp_dict.get("name")
    careers_domains = comp_dict.get("careers_domains") or []
    existing = comp_dict.get("ats_tenants") or []
    if existing:
        return  # already discovered (or hand-set) — don't re-crawl
    if not careers_domains:
        return  # nothing to crawl

    from app.adapters.ats.discovery import discover_for_competitor
    from app.fetcher import _fetch_raw_html
    from .models import Competitor

    tenants = discover_for_competitor(careers_domains, _fetch_raw_html)
    if not tenants:
        return

    # Persist to DB so the next scan picks them up via config.json sync.
    db = SessionLocal()
    try:
        c = db.query(Competitor).filter(Competitor.name == name).first()
        if c and not (c.ats_tenants or []):
            c.ats_tenants = tenants
            db.commit()
            print(f"[ats-discovery] {name}: {len(tenants)} tenant(s) -> {tenants}")
    finally:
        db.close()

    comp_dict["ats_tenants"] = tenants


def _scan_and_review_one(comp_dict: dict, topics: list, memory: dict,
                         company: str, industry: str, run_id: int) -> dict:
    """Run by a threadpool worker: scan ONE competitor, persist its findings,
    then synthesize its strategy review. Each worker has its own DB session so
    they don't contend on a shared transaction.

    Returns: {'name', 'findings', 'review_body', 'error'} — errors per
    competitor are isolated; one failure doesn't kill the batch."""
    import scanner as _scanner_mod
    from .models import Competitor, Finding as FindingModel
    from .competitor_reports import synthesize

    name = comp_dict["name"]
    out = {"name": name, "findings": [], "review_body": None, "error": None, "cancelled": False}

    if is_cancelled(run_id):
        out["cancelled"] = True
        print(f"[scan] skipping {name} — run cancelled")
        return out

    # 1a. Lazy ATS tenant discovery. Config.json only carries `ats_tenants`
    # for competitors that have already been discovered once (the sync
    # writes them back). For untouched rows we do a one-shot crawl of the
    # configured careers pages, extract canonical tenant prefixes (e.g.
    # boards.greenhouse.io/adeccogroup), and persist them so the scanner's
    # hiring sweep can scope to THIS competitor's own board instead of the
    # ATS root (which hosts every customer's jobs).
    #
    # Runs inside the worker so it parallelizes with the scan; failures are
    # isolated — a bad HTML extraction just leaves ats_tenants empty and
    # the sweep falls back to careers_domains-only scope.
    try:
        _ensure_ats_tenants_discovered(comp_dict)
    except Exception as e:
        print(f"[ats-discovery] ERROR for {name}: {e}")

    try:
        # 1b. Web search (blocks on Tavily HTTP; this is why we parallelize).
        print(f"[scan] {name}...")
        findings = _scanner_mod.scan_competitor(comp_dict, topics, memory)
        out["findings"] = findings
        print(f"[scan] {len(findings)} new items for {name}")
    except Exception as e:
        out["error"] = f"scan failed: {e}"
        print(f"[scan] ERROR {name}: {e}")
        return out

    if is_cancelled(run_id):
        out["cancelled"] = True
        print(f"[scan] skipping review for {name} — run cancelled")
        return out

    # 2. Persist findings + synthesize review, each worker on its own session.
    db = SessionLocal()
    try:
        c = db.query(Competitor).filter(Competitor.name == name).first()
        if not c:
            print(f"[review] skip {name} — not in DB")
            return out

        from app.signals.extract import classify as _classify
        from app.signals.llm_classify import classify_and_summarize as _llm_cs
        from app.signals.embed import embed_finding_text as _embed
        # First pass: filter to the findings we'll actually save (dedup on
        # hash). We do this before LLM calls so we never pay for a finding
        # we're about to skip. Classification + summary happens in one
        # Haiku call per finding (see llm_classify.py); regex fallback
        # kicks in if the LLM is unavailable or the response doesn't parse.
        to_enrich: list[tuple[dict, str]] = []
        for f in findings:
            content = f.get("content") or ""
            if not content:
                continue
            h = f.get("hash") or _scanner_mod.content_hash(content + (f.get("url") or ""))
            if db.query(FindingModel).filter(FindingModel.hash == h).first():
                continue
            to_enrich.append((f, h))

        # Second pass: fan out LLM calls in parallel. Each call returns
        # signal_type + materiality + summary together. The embedding
        # call is independent so we run it in the same worker — keeps
        # parallelism and total wall time the same.
        enriched: dict[str, tuple[str, float, dict, str | None, bytes | None, str | None]] = {}
        if to_enrich:
            from concurrent.futures import ThreadPoolExecutor
            def _one(item):
                f, h = item
                llm = _llm_cs(f, name)
                if llm is not None:
                    st, mat, payload, summary = (
                        llm["signal_type"], llm["materiality"],
                        llm["payload"], llm["summary"])
                else:
                    # Fallback to deterministic regex classifier; no summary.
                    st, mat, payload = _classify(f)
                    summary = None
                emb_bytes, emb_model = _embed(f.get("title"), summary, f.get("content"))
                return h, (st, mat, payload, summary, emb_bytes, emb_model)
            with ThreadPoolExecutor(max_workers=min(8, len(to_enrich))) as pool:
                for h, tup in pool.map(_one, to_enrich):
                    enriched[h] = tup

        for f, h in to_enrich:
            st, mat, payload, summary, emb_bytes, emb_model = enriched[h]
            db.add(FindingModel(
                run_id=run_id,
                competitor=name,
                source=f.get("source", ""),
                topic=f.get("topic"),
                title=f.get("title"),
                url=f.get("url"),
                content=f.get("content") or "",
                summary=summary,
                hash=h,
                search_provider=f.get("search_provider"),
                score=_coerce_score(f.get("relevance")),
                published_at=_parse_published(f.get("published")),
                signal_type=st,
                materiality=mat,
                payload=payload,
                matched_keyword=f.get("matched_keyword"),
                embedding=emb_bytes,
                embedding_model=emb_model,
            ))
            _emit_material_event(db, run_id, f, c.id, st, mat)
        db.commit()

        # 3. Competitor strategy review — includes the just-saved findings.
        try:
            print(f"[review] {name}...")
            cr = synthesize(db, c, run_id=run_id, company=company, industry=industry)
            if cr:
                out["review_body"] = cr.body_md
                print(f"[review] {name} done")
            else:
                print(f"[review] {name} skipped (no material)")
        except Exception as e:
            out["error"] = f"review failed: {e}"
            print(f"[review] ERROR {name}: {e}")
    finally:
        db.close()

    return out


def run_scan_job(
    triggered_by: str = "schedule",
    freshness_days: int | None = None,
    run_id: int | None = None,
):
    """Per-competitor pipeline, then market summary:
      1. For each competitor (in parallel): scan → save findings → synthesize review
      2. Stuff fresh reviews into memory so the market digest has synthesized context
      3. Market summary (one Claude call)
      4. Save Report, email team

    Competitor pages go live as each worker completes; the market digest is the
    final synthesis across all of them.

    `run_id` is supplied by the queue drainer so we re-use the existing row;
    cron / direct callers pass nothing and get a fresh row.
    """
    run, db = _start_run("scan", triggered_by, run_id=run_id)
    token = current_run_id.set(run.id)
    try:
        from service import load_config, build_digest
        from scanner import load_memory, save_memory
        from analyzer import analyze_findings
        from competitor_manager import record_scan_activity
        from concurrent.futures import ThreadPoolExecutor, as_completed

        _log(db, run, "loading config")
        config = load_config()
        memory = load_memory()

        days = _compute_freshness(db, freshness_days)
        from app.search_providers import tavily as _tavily
        _tavily.TAVILY_DAYS = days
        _log(db, run, f"freshness window: last {days} day{'s' if days != 1 else ''} "
                     f"({'explicit' if freshness_days is not None else 'auto since last scan'})")

        competitors = list(config["competitors"])
        concurrency = int(os.environ.get("SCAN_CONCURRENCY", "4"))
        company = config.get("company", "Seek")
        industry = config.get("industry", "job search and recruitment platforms")

        all_findings: list[dict] = []
        fresh_reviews: dict[str, str] = {}  # competitor_name → latest review body

        with _StreamToRunEvents(run.id) as tee, contextlib.redirect_stdout(tee):
            print(f"[pipeline] {len(competitors)} competitors · concurrency={concurrency}")
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = {
                    pool.submit(_scan_and_review_one,
                                comp, config["watch_topics"], memory,
                                company, industry, run.id): comp["name"]
                    for comp in competitors
                }
                for fut in as_completed(futures):
                    result = fut.result()  # never raises — _scan_and_review_one catches
                    all_findings.extend(result["findings"])
                    if result["review_body"]:
                        fresh_reviews[result["name"]] = result["review_body"]
                    if is_cancelled(run.id):
                        # Cancel anything not yet started; in-flight workers will
                        # see the flag at their own checkpoint and bail.
                        for f in futures:
                            f.cancel()

            check_cancel(run.id)
            print(f"[pipeline] competitor scans done · {len(all_findings)} findings · "
                  f"{len(fresh_reviews)} reviews refreshed")

            # Threat angles were updated on each Competitor row inside the
            # workers. Mirror them back into config.json so the next scan's
            # scanner reads the fresh _threat_angle values.
            try:
                from .config_sync import sync_db_to_config
                sync_db_to_config(db)
                print("[pipeline] config.json synced with fresh threat angles")
            except Exception as e:
                print(f"[pipeline] config sync failed (non-fatal): {e}")

            # Customer-side aggregate sweep: category discussion across
            # subreddits + twitter. Uses same Tavily client + memory dedup.
            try:
                from .customer_watch import scan_customer_sources
                from .models import Finding as FindingModel
                cust_findings = scan_customer_sources(config, memory)
                if cust_findings:
                    from app.signals.extract import classify as _classify
                    from app.signals.llm_classify import classify_and_summarize as _llm_cs
                    from app.signals.embed import embed_finding_text as _embed
                    for f in cust_findings:
                        if db.query(FindingModel).filter(
                            FindingModel.hash == f["hash"]
                        ).first():
                            continue
                        llm = _llm_cs(f, f["competitor"])
                        if llm is not None:
                            st, mat, payload, summary = (
                                llm["signal_type"], llm["materiality"],
                                llm["payload"], llm["summary"])
                        else:
                            st, mat, payload = _classify(f)
                            summary = None
                        emb_bytes, emb_model = _embed(f.get("title"), summary, f.get("content"))
                        db.add(FindingModel(
                            run_id=run.id,
                            competitor=f["competitor"],
                            source=f["source"],
                            topic=f["topic"],
                            title=f.get("title"),
                            url=f.get("url"),
                            content=f.get("content"),
                            summary=summary,
                            hash=f["hash"],
                            search_provider=f.get("search_provider"),
                            score=_coerce_score(f.get("relevance")),
                            published_at=_parse_published(f.get("published")),
                            signal_type=st,
                            materiality=mat,
                            payload=payload,
                            matched_keyword=f.get("matched_keyword"),
                            embedding=emb_bytes,
                            embedding_model=emb_model,
                        ))
                    db.commit()
                    all_findings.extend(cust_findings)
                    print(f"[pipeline] {len(cust_findings)} customer-discussion findings saved")
            except Exception as e:
                print(f"[customer] sweep failed (non-fatal): {e}")

            try:
                record_scan_activity(all_findings, config)
            except Exception as e:
                print(f"[activity] tracking failed (non-fatal): {e}")

            # Refresh company + customer briefs so the market digest reads the
            # freshest view of "us" and "our customers" alongside competitor
            # reviews. A single shared DB session is fine here — sequential.
            try:
                from .context_briefs import synthesize as synth_context
                for scope in ("company", "customer"):
                    print(f"[brief] synthesizing {scope}...")
                    cb = synth_context(db, scope, run_id=run.id,
                                       company=company, industry=industry)
                    if cb:
                        print(f"[brief] {scope} done")
                    else:
                        print(f"[brief] {scope} skipped (no inputs yet)")
            except Exception as e:
                print(f"[brief] batch failed (non-fatal): {e}")

            # Inject fresh reviews into memory so the market digest reads them
            # as "Competitor Profiles (accumulated knowledge)" — no analyzer edit.
            existing_profiles = memory.get("competitor_profiles", {}) or {}
            memory["competitor_profiles"] = {**existing_profiles, **fresh_reviews}

            # Also inject company + customer briefs into memory so the analyzer
            # picks them up as context. We slot them under pseudo-competitor
            # names so they render in the same "accumulated knowledge" block.
            from .context_briefs import current as current_brief
            for scope, label in (("company", f"__{company}"), ("customer", "__Customers")):
                cb = current_brief(db, scope)
                if cb and cb.body_md:
                    memory["competitor_profiles"][label] = cb.body_md

            print("[analyze] generating market summary")
            analysis, features = analyze_findings(all_findings, config, memory)
            print(f"[analyze] digest referenced {len(features)} findings with threat levels")
            _stamp_digest_threat_levels(db, run.id, features)
            print("[analyze] building digest")
            digest = build_digest(all_findings, analysis, memory)

            try:
                save_memory(memory)
            except Exception as e:
                print(f"[memory] save failed (non-fatal): {e}")

        report = Report(
            run_id=run.id,
            title=f"Scan {run.started_at:%Y-%m-%d %H:%M}",
            body_md=digest,
        )
        db.add(report)
        db.flush()
        run.report_id = report.id
        run.findings_count = len(all_findings)
        db.commit()

        try:
            from mailer import send_digest_to_team
            with _StreamToRunEvents(run.id) as _tee, contextlib.redirect_stdout(_tee):
                send_digest_to_team(digest, config, all_findings)
            _log(db, run, "digest emailed")
        except Exception as e:
            _log(db, run, f"email skipped: {e}", "warn")

        _finish_run(db, run, "ok")
    except RunCancelled:
        _log(db, run, "cancelled by user", "warn")
        _finish_run(db, run, "cancelled")
    except Exception as e:
        tb = traceback.format_exc()
        _log(db, run, f"ERROR: {e}\n{tb}", "error")
        _finish_run(db, run, "error", str(e))
    finally:
        clear_cancel(run.id)
        current_run_id.reset(token)


def _finding_row_to_dict(f: Finding) -> dict:
    """Shape a Finding ORM row into the dict shape analyzer/build_digest expect."""
    return {
        "competitor": f.competitor,
        "source": f.source,
        "topic": f.topic or "",
        "title": f.title or "",
        "url": f.url or "",
        "content": (f.content or f.summary or "")[:6000],
    }


def run_market_digest_job(triggered_by: str = "manual", run_id: int | None = None):
    """Regenerate the market digest from existing findings — no new scraping.

    Uses the most recent completed scan's findings if available, otherwise
    falls back to the last 14 days across all runs. Seeds memory with the
    current competitor reviews + context briefs so the digest reads the same
    synthesized view a scan would have. Writes a Report row and logs progress
    as a Run with kind='market_digest' so the live panel + /runs show it.

    `run_id` is supplied by the queue drainer when this job is run via
    enqueue; direct callers omit it.
    """
    run, db = _start_run("market_digest", triggered_by, run_id=run_id)
    # Cache primitives so the finally block never touches an expired ORM row
    # after _finish_run closed the session.
    run_id = run.id
    run_started_at = run.started_at
    token = current_run_id.set(run_id)
    try:
        from service import load_config, build_digest
        from scanner import load_memory
        from analyzer import analyze_findings
        from .models import CompetitorReport, Competitor as CompetitorModel
        from datetime import timedelta

        _log(db, run, "loading config + memory")
        config = load_config()
        memory = load_memory()
        company = config.get("company", "Seek")
        industry = config.get("industry", "job search and recruitment platforms")

        with _StreamToRunEvents(run_id) as tee, contextlib.redirect_stdout(tee):
            last_scan = (
                db.query(Run)
                .filter(Run.kind.in_(["scan", "customer_scan"]))
                .filter(Run.status == "ok")
                .order_by(Run.started_at.desc())
                .first()
            )
            if last_scan:
                rows = (
                    db.query(Finding)
                    .filter(Finding.run_id == last_scan.id)
                    .order_by(Finding.created_at.desc())
                    .all()
                )
                print(f"[digest] {len(rows)} findings from scan run #{last_scan.id}")
            else:
                rows = []

            if not rows:
                since = datetime.utcnow() - timedelta(days=14)
                rows = (
                    db.query(Finding)
                    .filter(Finding.created_at >= since)
                    .order_by(Finding.created_at.desc())
                    .limit(200)
                    .all()
                )
                print(f"[digest] fallback: {len(rows)} findings from last 14 days")

            all_findings = [_finding_row_to_dict(r) for r in rows]

            # Seed memory with current competitor reviews so the analyzer reads
            # them as "accumulated knowledge" — same pattern as run_scan_job.
            existing_profiles = memory.get("competitor_profiles", {}) or {}
            fresh_reviews: dict[str, str] = {}
            competitors = db.query(CompetitorModel).filter(
                CompetitorModel.active == True  # noqa: E712
            ).all()
            for c in competitors:
                latest = (
                    db.query(CompetitorReport)
                    .filter(CompetitorReport.competitor_id == c.id)
                    .order_by(CompetitorReport.created_at.desc())
                    .first()
                )
                if latest and latest.body_md:
                    fresh_reviews[c.name] = latest.body_md
            memory["competitor_profiles"] = {**existing_profiles, **fresh_reviews}

            from .context_briefs import current as current_brief
            for scope, label in (("company", f"__{company}"), ("customer", "__Customers")):
                cb = current_brief(db, scope)
                if cb and cb.body_md:
                    memory["competitor_profiles"][label] = cb.body_md

            print(f"[digest] seeded memory with {len(fresh_reviews)} reviews + context briefs")
            print("[analyze] generating market summary")
            # Retry the Claude call once on transient connection errors —
            # cheap insurance since we've already spent the prompt-build effort.
            try:
                analysis, features = analyze_findings(all_findings, config, memory)
            except Exception as e:
                print(f"[analyze] first attempt failed ({e}); retrying once")
                analysis, features = analyze_findings(all_findings, config, memory)
            print(f"[analyze] digest referenced {len(features)} findings with threat levels")
            _stamp_digest_threat_levels(db, run_id, features)
            print("[analyze] building digest")
            digest = build_digest(all_findings, analysis, memory)

        report = Report(
            run_id=run_id,
            title=f"Digest {run_started_at:%Y-%m-%d %H:%M}",
            body_md=digest,
        )
        db.add(report)
        db.flush()
        # Refresh the attached Run row by id — the original instance may have
        # expired after commits/flushes inside the pipeline.
        run = db.get(Run, run_id)
        run.report_id = report.id
        run.findings_count = len(all_findings)
        db.commit()

        _finish_run(db, run, "ok")
    except RunCancelled:
        run = db.get(Run, run_id) or run
        _log(db, run, "cancelled by user", "warn")
        _finish_run(db, run, "cancelled")
    except Exception as e:
        tb = traceback.format_exc()
        run = db.get(Run, run_id) or run
        _log(db, run, f"ERROR: {e}\n{tb}", "error")
        _finish_run(db, run, "error", str(e))
    finally:
        clear_cancel(run_id)
        current_run_id.reset(token)


def run_competitor_scan_job(competitor_id: int, triggered_by: str = "manual",
                            freshness_days: int | None = None):
    """Scoped end-to-end scan for a SINGLE competitor — scrape + save findings
    + synthesize strategy review. No market digest, no email, no company/customer
    briefs. For when you want to refresh one competitor on demand without
    running the whole pipeline.

    Recorded as a Run with kind='competitor_scan' so it shows up on /runs,
    streams via the live panel, and the status bar reflects progress globally.
    """
    from .models import Competitor as CompetitorModel

    run, db = _start_run("competitor_scan", triggered_by)
    token = current_run_id.set(run.id)
    try:
        from service import load_config
        from scanner import load_memory, save_memory

        competitor = db.get(CompetitorModel, competitor_id)
        if not competitor:
            raise ValueError(f"competitor #{competitor_id} not found")

        _log(db, run, f"scoped scan for {competitor.name}")
        config = load_config()
        memory = load_memory()

        # The scanner reads the competitor dict from config.json (keywords,
        # newsroom_domains, subreddits, etc.) — not the DB row — so we grab
        # the matching entry. config_sync keeps this in lockstep with the DB.
        comp_dict = next(
            (c for c in config.get("competitors", []) if c.get("name") == competitor.name),
            None,
        )
        if not comp_dict:
            raise ValueError(
                f"{competitor.name} not present in config.json — try editing "
                "then saving any field on /admin/competitors to trigger a re-sync."
            )

        days = _compute_freshness(db, freshness_days)
        from app.search_providers import tavily as _tavily
        _tavily.TAVILY_DAYS = days
        _log(db, run, f"freshness window: last {days} day{'s' if days != 1 else ''}")

        company = config.get("company", "Seek")
        industry = config.get("industry", "job search and recruitment platforms")

        with _StreamToRunEvents(run.id) as tee, contextlib.redirect_stdout(tee):
            print(f"[competitor-scan] {competitor.name} · freshness={days}d")
            result = _scan_and_review_one(
                comp_dict, config.get("watch_topics", []), memory,
                company, industry, run.id,
            )
            print(
                f"[competitor-scan] done — findings={len(result['findings'])} "
                f"review={'yes' if result['review_body'] else 'no'}"
            )
            if result.get("error"):
                print(f"[competitor-scan] error detail: {result['error']}")

            try:
                save_memory(memory)
            except Exception as e:
                print(f"[memory] save failed (non-fatal): {e}")

        run.findings_count = len(result["findings"])
        db.commit()

        # Mirror any threat-angle updates written by the synthesizer back to
        # config.json so the next full scan sees them.
        try:
            from .config_sync import sync_db_to_config
            sync_db_to_config(db)
        except Exception as e:
            _log(db, run, f"config sync failed (non-fatal): {e}", "warn")

        if result.get("cancelled") or is_cancelled(run.id):
            _log(db, run, "cancelled by user", "warn")
            _finish_run(db, run, "cancelled")
        else:
            status_final = "error" if result.get("error") else "ok"
            _finish_run(db, run, status_final,
                        error=result.get("error") if status_final == "error" else None)
    except Exception as e:
        tb = traceback.format_exc()
        _log(db, run, f"ERROR: {e}\n{tb}", "error")
        _finish_run(db, run, "error", str(e))
    finally:
        clear_cancel(run.id)
        current_run_id.reset(token)


def run_customer_scan_job(triggered_by: str = "manual",
                          freshness_days: int | None = None):
    """Customer-side aggregated scan only: sweep subreddits + twitter for
    category-level discussion, save findings, refresh the customer brief.

    Mirrors `run_competitor_scan_job` in shape — creates a Run with
    kind='customer_scan', reuses `scanner.search_tavily` via
    `scan_customer_sources` (same TAVILY_DAYS freshness knob), streams logs,
    and supports cooperative cancel.
    """
    from .customer_watch import scan_customer_sources
    from .models import Finding as FindingModel

    run, db = _start_run("customer_scan", triggered_by)
    token = current_run_id.set(run.id)
    try:
        from service import load_config
        from scanner import load_memory, save_memory

        _log(db, run, "customer aggregate scan")
        config = load_config()
        memory = load_memory()

        days = _compute_freshness(db, freshness_days)
        from app.search_providers import tavily as _tavily
        _tavily.TAVILY_DAYS = days
        _log(db, run, f"freshness window: last {days} day{'s' if days != 1 else ''} "
                     f"({'explicit' if freshness_days is not None else 'auto since last scan'})")

        company = config.get("company", "Seek")
        industry = config.get("industry", "job search and recruitment platforms")

        cust_findings: list[dict] = []
        with _StreamToRunEvents(run.id) as tee, contextlib.redirect_stdout(tee):
            print(f"[customer-scan] freshness={days}d")
            check_cancel(run.id)
            cust_findings = scan_customer_sources(config, memory)
            if cust_findings:
                from app.signals.extract import classify as _classify
                from app.signals.llm_classify import classify_and_summarize as _llm_cs
                from app.signals.embed import embed_finding_text as _embed
                for f in cust_findings:
                    if db.query(FindingModel).filter(
                        FindingModel.hash == f["hash"]
                    ).first():
                        continue
                    llm = _llm_cs(f, f["competitor"])
                    if llm is not None:
                        st, mat, payload, summary = (
                            llm["signal_type"], llm["materiality"],
                            llm["payload"], llm["summary"])
                    else:
                        st, mat, payload = _classify(f)
                        summary = None
                    emb_bytes, emb_model = _embed(f.get("title"), summary, f.get("content"))
                    db.add(FindingModel(
                        run_id=run.id,
                        competitor=f["competitor"],
                        source=f["source"],
                        topic=f["topic"],
                        title=f.get("title"),
                        url=f.get("url"),
                        content=f.get("content"),
                        summary=summary,
                        hash=f["hash"],
                        search_provider=f.get("search_provider"),
                        score=_coerce_score(f.get("relevance")),
                        published_at=_parse_published(f.get("published")),
                        signal_type=st,
                        materiality=mat,
                        payload=payload,
                        matched_keyword=f.get("matched_keyword"),
                        embedding=emb_bytes,
                        embedding_model=emb_model,
                    ))
                db.commit()
                print(f"[customer-scan] {len(cust_findings)} findings saved")
            else:
                print("[customer-scan] 0 new findings")

            check_cancel(run.id)

            # Refresh the customer brief so the just-scraped discussion lands
            # in the synthesized view immediately.
            try:
                from .context_briefs import synthesize as synth_context
                print("[brief] synthesizing customer...")
                cb = synth_context(db, "customer", run_id=run.id,
                                   company=company, industry=industry)
                if cb:
                    print("[brief] customer done")
                else:
                    print("[brief] customer skipped (no inputs yet)")
            except Exception as e:
                print(f"[brief] failed (non-fatal): {e}")

            try:
                save_memory(memory)
            except Exception as e:
                print(f"[memory] save failed (non-fatal): {e}")

        run.findings_count = len(cust_findings)
        db.commit()

        if is_cancelled(run.id):
            _log(db, run, "cancelled by user", "warn")
            _finish_run(db, run, "cancelled")
        else:
            _finish_run(db, run, "ok")
    except RunCancelled:
        _log(db, run, "cancelled by user", "warn")
        _finish_run(db, run, "cancelled")
    except Exception as e:
        tb = traceback.format_exc()
        _log(db, run, f"ERROR: {e}\n{tb}", "error")
        _finish_run(db, run, "error", str(e))
    finally:
        clear_cancel(run.id)
        current_run_id.reset(token)


def run_reply_check_job():
    run, db = _start_run("reply_check", "schedule")
    token = current_run_id.set(run.id)
    try:
        from service import load_config, process_replies
        config = load_config()
        with _StreamToRunEvents(run.id) as _tee, contextlib.redirect_stdout(_tee):
            process_replies(config)
        _log(db, run, "reply check complete")
        _finish_run(db, run, "ok")
    except Exception as e:
        tb = traceback.format_exc()
        _log(db, run, f"ERROR: {e}\n{tb}", "error")
        _finish_run(db, run, "error", str(e))
    finally:
        current_run_id.reset(token)


def run_discovery_job(run_id: int | None = None):
    run, db = _start_run("discovery", "manual", run_id=run_id)
    token = current_run_id.set(run.id)
    try:
        from service import load_config
        from competitor_manager import run_discovery, prune_competitors
        config = load_config()
        with _StreamToRunEvents(run.id) as _tee, contextlib.redirect_stdout(_tee):
            disc = run_discovery(config)
            prune = prune_competitors(config)
        _log(db, run, f"discovery added={len(disc.get('added', []))} pruned={len(prune.get('pruned', []))}")
        _finish_run(db, run, "ok")
    except Exception as e:
        tb = traceback.format_exc()
        _log(db, run, f"ERROR: {e}\n{tb}", "error")
        _finish_run(db, run, "error", str(e))
    finally:
        current_run_id.reset(token)


def rebuild_user_preferences_job(user_id: int) -> None:
    """Wrapper around app.ranker.rollup.rebuild_user_preferences for use
    by APScheduler. Opens its own session — schedulers run detached
    from any request context.

    Errors are logged but not raised: a crash inside one user's rollup
    shouldn't stop the scheduler or subsequent runs. The rollup can
    always be re-triggered on the user's next explicit signal or the
    next nightly sweep.
    """
    from .ranker.rollup import rebuild_user_preferences
    db = SessionLocal()
    try:
        summary = rebuild_user_preferences(db, user_id)
        print(
            f"  [rollup] user={user_id} events={summary['events_considered']} "
            f"keys={summary['keys_written']} event_count_30d={summary['event_count_30d']}",
            flush=True,
        )
    except Exception as e:
        print(f"  [rollup] user={user_id} failed: {e}", flush=True)
    finally:
        db.close()


def nightly_rebuild_preferences_job() -> None:
    """Iterate active users and rebuild each one's preference vector.
    Single-threaded (SQLite is single-writer; parallelism would just
    serialize at the DB layer). Budget per spec 02: ≤5min for 100 users.
    """
    from .models import User
    db = SessionLocal()
    try:
        user_ids = [uid for (uid,) in db.query(User.id).filter(User.is_active == True).all()]  # noqa: E712
    finally:
        db.close()
    print(f"  [rollup] nightly sweep: {len(user_ids)} users", flush=True)
    for uid in user_ids:
        rebuild_user_preferences_job(uid)
    print("  [rollup] nightly sweep done", flush=True)


def prune_signal_events(retention_days: int = 180) -> int:
    """Nightly maintenance: delete UserSignalEvent rows older than the
    retention window. Part of the ranker's signal log
    (docs/ranker/01-signal-log.md).

    Rationale: the preference rollup (spec 02) uses a 30-day decay
    half-life, so events older than 180d contribute <1% to any score.
    Keeping them just bloats the table. The rollup outputs in
    user_preferences_vector are forever; the raw event log is a
    re-derivable cache.

    Not wrapped in a Run — this is plain maintenance, no user-facing
    pipeline. Returns count deleted. Safe to call with retention_days=0
    to purge everything (don't).
    """
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(days=retention_days)
    db = SessionLocal()
    try:
        deleted = (
            db.query(UserSignalEvent)
            .filter(UserSignalEvent.ts < cutoff)
            .delete(synchronize_session=False)
        )
        db.commit()
        print(
            f"  [prune_signal_events] deleted {deleted} rows older than "
            f"{retention_days} days (cutoff {cutoff.isoformat()})",
            flush=True,
        )
        return deleted
    except Exception as e:
        db.rollback()
        print(f"  [prune_signal_events] failed: {e}", flush=True)
        return 0
    finally:
        db.close()


def run_positioning_refresh_job():
    """Monthly sweep: extract positioning pillars for every active competitor.

    Unlike the scan/momentum jobs, this doesn't open a Run — positioning
    extraction is an out-of-band signal pipeline that fails soft per
    competitor. Logging is via print() into the scheduler's stdout.
    Sleeps 60s between competitors to stay gentle on the LLM + scrapers.
    """
    import time
    from .models import Competitor
    from .signals.positioning import extract_positioning

    db = SessionLocal()
    try:
        active = (
            db.query(Competitor)
            .filter(Competitor.active == True)
            .order_by(Competitor.name)
            .all()
        )
        print(
            f"  [positioning_refresh] sweeping {len(active)} active competitors",
            flush=True,
        )
        for i, c in enumerate(active):
            try:
                extract_positioning(c, db)
            except Exception as e:
                print(
                    f"  [positioning_refresh] {c.name}: {type(e).__name__}: {e}",
                    flush=True,
                )
            if i < len(active) - 1:
                time.sleep(60)
    finally:
        db.close()


DEEP_RESEARCH_TIMEOUT_S = int(os.environ.get("DEEP_RESEARCH_TIMEOUT_S", str(30 * 60)))
DEEP_RESEARCH_MAX_CONCURRENT = int(os.environ.get("DEEP_RESEARCH_MAX_CONCURRENT", "2"))
# Poll cadence inside the job wrapper. Gemini's Interactions API returns
# 'running' quickly; bumping past 20s adds latency without saving cost.
_DEEP_RESEARCH_POLL_S = int(os.environ.get("DEEP_RESEARCH_POLL_S", "20"))


def current_research_load(db) -> int:
    """Count DeepResearchReport rows currently in flight. Used by the
    route layer to enforce the concurrency cap."""
    return (
        db.query(DeepResearchReport)
        .filter(DeepResearchReport.status.in_(["queued", "running"]))
        .count()
    )


def _build_research_brief(competitor, config: dict) -> str:
    """Fill the deep_research_brief skill with competitor + company context."""
    from .skills import load_active
    template = load_active("deep_research_brief") or ""
    topics = ", ".join(config.get("watch_topics") or []) or "general strategy"
    vals = {
        "competitor_name": competitor.name or "",
        "category":        competitor.category or "unspecified",
        "our_company":     config.get("company", "Seek"),
        "our_industry":    config.get("industry", "job search and recruitment platforms"),
        "threat_angle":    competitor.threat_angle or "unspecified",
        "watch_topics":    topics,
    }
    out = template
    for k, v in vals.items():
        out = out.replace("{{" + k + "}}", v)
    return out


def _persist_research_terminal(db, report: DeepResearchReport, normalized: dict) -> None:
    """Shared commit path for both the happy path and resume-on-boot.
    Writes the report's terminal state and closes the companion Run if any."""
    report.status = normalized["status"]
    report.body_md = normalized.get("body_md") or ""
    report.sources = normalized.get("sources") or []
    report.error = normalized.get("error")
    report.model = normalized.get("model") or report.model
    report.cost_usd = normalized.get("cost_usd")
    report.finished_at = datetime.utcnow()
    db.commit()


def _poll_research_to_terminal(
    db,
    report: DeepResearchReport,
    run: Run | None,
    deadline: datetime,
) -> str:
    """Poll Gemini until the report reaches a terminal state or the deadline
    passes. Returns the final status string."""
    from .adapters import gemini_research as _gem
    import time as _time

    while True:
        if datetime.utcnow() >= deadline:
            report.status = "failed"
            report.error = (
                f"Timed out after {DEEP_RESEARCH_TIMEOUT_S}s waiting for Gemini. "
                "The task may still complete on Gemini's side; we stopped polling."
            )
            report.finished_at = datetime.utcnow()
            db.commit()
            if run is not None:
                _log(db, run, report.error, "warn")
            return "failed"

        try:
            result = _gem.poll_research(report.interaction_id)
        except _gem.GeminiUnavailable as e:
            # Config-level failure — don't retry, mark failed and bail.
            report.status = "failed"
            report.error = str(e)
            report.finished_at = datetime.utcnow()
            db.commit()
            if run is not None:
                _log(db, run, f"research failed: {e}", "error")
            return "failed"

        if result.get("_transient_error") and run is not None:
            # Network blip — log quietly and keep polling.
            _log(db, run, f"poll transient: {result['_transient_error']}", "warn")

        if result["status"] in ("ready", "failed"):
            _persist_research_terminal(db, report, result)
            if run is not None:
                if result["status"] == "ready":
                    meta = {
                        "competitor_id": report.competitor_id,
                        "report_id": report.id,
                    }
                    # Look up the competitor name once for the live-log link.
                    from .models import Competitor as _C
                    c = db.get(_C, report.competitor_id)
                    if c:
                        meta["competitor_name"] = c.name
                    db.add(RunEvent(
                        run_id=run.id,
                        level="material",
                        message=f"[research] {c.name if c else 'competitor'} dossier ready",
                        meta=meta,
                    ))
                    db.commit()
                    _log(db, run, "research ready")
                else:
                    _log(db, run, f"research failed: {report.error or 'unknown'}", "error")
            return result["status"]

        _time.sleep(_DEEP_RESEARCH_POLL_S)


def run_deep_research_job(
    competitor_id: int,
    report_id: int,
    agent: str = "preview",
    triggered_by: str = "manual",
) -> None:
    """Background job: kick off a Gemini Deep Research interaction for one
    competitor and poll it to completion. Creates + closes a Run row so the
    run shows up in /runs like any other pipeline.

    `report_id` is the pre-created DeepResearchReport row the route layer
    inserted in 'queued' state — this wrapper fills in interaction_id,
    drives status transitions, and writes the final body + sources.
    """
    from .adapters import gemini_research as _gem
    from .models import Competitor as _Competitor

    run, db = _start_run("deep_research", triggered_by)
    token = current_run_id.set(run.id)
    deadline = datetime.utcnow() + timedelta(seconds=DEEP_RESEARCH_TIMEOUT_S)
    try:
        # Read config.json directly — don't `from service import load_config`.
        # service.py aborts the process on missing ANTHROPIC_API_KEY at import
        # time, which would kill this worker thread. Deep research only needs
        # the company / industry / watch_topics strings, so a plain JSON read
        # is sufficient.
        import json as _json
        cfg_path = os.environ.get("CONFIG_PATH", "config.json")
        try:
            with open(cfg_path) as _f:
                config = _json.load(_f)
        except Exception:
            config = {}

        competitor = db.get(_Competitor, competitor_id)
        report = db.get(DeepResearchReport, report_id)
        if competitor is None or report is None:
            raise ValueError(
                f"deep-research job could not locate competitor={competitor_id} "
                f"report={report_id}"
            )

        report.run_id = run.id
        # If the route layer already built the brief, keep it — otherwise
        # build one now from the current skill + config.
        if not report.brief:
            report.brief = _build_research_brief(competitor, config)
        db.commit()

        _log(db, run, f"deep research for {competitor.name} (agent={agent})")

        try:
            iid = _gem.start_research(report.brief, agent=agent)
        except _gem.GeminiUnavailable as e:
            report.status = "failed"
            report.error = str(e)
            report.finished_at = datetime.utcnow()
            db.commit()
            _log(db, run, f"research failed: {e}", "error")
            _finish_run(db, run, "error", str(e))
            return

        report.interaction_id = iid
        report.status = "running"
        db.commit()
        _log(db, run, f"interaction started: {iid}")

        final = _poll_research_to_terminal(db, report, run, deadline)
        _finish_run(db, run, "ok" if final == "ready" else "error",
                    error=report.error if final != "ready" else None)
    except Exception as e:
        tb = traceback.format_exc()
        _log(db, run, f"ERROR: {e}\n{tb}", "error")
        # Try to surface the error on the report row too, if it exists.
        try:
            r = db.get(DeepResearchReport, report_id)
            if r and r.status not in ("ready", "failed"):
                r.status = "failed"
                r.error = f"{type(e).__name__}: {e}"
                r.finished_at = datetime.utcnow()
                db.commit()
        except Exception:
            pass
        _finish_run(db, run, "error", str(e))
    finally:
        current_run_id.reset(token)


def resume_in_flight_research() -> int:
    """Startup sweep: any DeepResearchReport left in queued/running at boot
    needs to be resumed. For rows with an interaction_id, poll Gemini once
    — if it's done, persist the result; if Gemini has lost it, mark failed;
    else kick a new background poller. Rows without an interaction_id
    didn't make it past start_research and are marked failed.

    Returns the count of rows touched. Logs quietly; never raises."""
    from .adapters import gemini_research as _gem
    import threading as _threading

    db = SessionLocal()
    touched = 0
    try:
        rows = (
            db.query(DeepResearchReport)
            .filter(DeepResearchReport.status.in_(["queued", "running"]))
            .all()
        )
        for r in rows:
            touched += 1
            if not r.interaction_id:
                r.status = "failed"
                r.error = "interrupted before Gemini returned an interaction id"
                r.finished_at = datetime.utcnow()
                db.commit()
                continue
            try:
                result = _gem.poll_research(r.interaction_id)
            except _gem.GeminiUnavailable as e:
                r.status = "failed"
                r.error = str(e)
                r.finished_at = datetime.utcnow()
                db.commit()
                continue
            if result["status"] in ("ready", "failed"):
                _persist_research_terminal(db, r, result)
                continue
            # Still running on Gemini's side — resume polling in a daemon
            # thread so boot doesn't block. No new Run is opened; the
            # original Run (if any) is already closed as 'error' by
            # _reap_orphan_runs on restart, and resuming it is more
            # confusing than just letting the report update standalone.
            r.status = "running"
            db.commit()
            rid = r.id
            _threading.Thread(
                target=_resume_research_poller,
                args=(rid,),
                name=f"research-resume-{rid}",
                daemon=True,
            ).start()
    except Exception as e:
        print(f"  [research-resume] sweep failed: {e}", flush=True)
    finally:
        db.close()
    return touched


def _resume_research_poller(report_id: int) -> None:
    """Thread target for resuming a research poll after a server restart.
    No Run row (see resume_in_flight_research rationale); just drive the
    DeepResearchReport row to terminal state."""
    db = SessionLocal()
    try:
        r = db.get(DeepResearchReport, report_id)
        if r is None or r.status not in ("queued", "running"):
            return
        deadline = datetime.utcnow() + timedelta(seconds=DEEP_RESEARCH_TIMEOUT_S)
        _poll_research_to_terminal(db, r, None, deadline)
    except Exception as e:
        print(f"  [research-resume] poller {report_id} failed: {e}", flush=True)
    finally:
        db.close()


# ── Market synthesis (Spec 05) ──────────────────────────────────────
# Cross-competitor Gemini Deep Research. Reuses the adapter + persist
# helper from DR; the job wrapper, poll loop, and resume-on-boot are
# mirrored here rather than folded into DR to keep each path auditable
# and avoid introducing a kind-switch inside the DR helpers.

MARKET_SYNTHESIS_TIMEOUT_S = int(
    os.environ.get("MARKET_SYNTHESIS_TIMEOUT_S", str(30 * 60))
)
_MARKET_SYNTHESIS_POLL_S = int(
    os.environ.get("MARKET_SYNTHESIS_POLL_S", "20")
)


def current_synthesis_load(db) -> int:
    """Count in-flight MarketSynthesisReport rows. v1 enforces at-most-1
    globally — both the route and the cron respect that lock, so this
    returns 0 or 1."""
    return (
        db.query(MarketSynthesisReport)
        .filter(MarketSynthesisReport.status.in_(["queued", "running"]))
        .count()
    )


def current_discovery_load(db) -> int:
    """Count discover_competitors Runs currently in flight. Route layer
    uses this to enforce the one-at-a-time cap."""
    return (
        db.query(Run)
        .filter(Run.kind == "discover_competitors", Run.status == "running")
        .count()
    )


def _poll_synthesis_to_terminal(
    db,
    report: "MarketSynthesisReport",
    run: Run | None,
    deadline: datetime,
) -> str:
    """Poll Gemini for a synthesis row until it reaches a terminal state or
    the deadline passes. Mirrors `_poll_research_to_terminal` but emits a
    synthesis-flavoured material event on completion. Returns the final
    status string."""
    from .adapters import gemini_research as _gem
    import time as _time

    while True:
        if datetime.utcnow() >= deadline:
            report.status = "failed"
            report.error = (
                f"Timed out after {MARKET_SYNTHESIS_TIMEOUT_S}s waiting for Gemini. "
                "The task may still complete on Gemini's side; we stopped polling."
            )
            report.finished_at = datetime.utcnow()
            db.commit()
            if run is not None:
                _log(db, run, report.error, "warn")
            return "failed"

        try:
            result = _gem.poll_research(report.interaction_id)
        except _gem.GeminiUnavailable as e:
            report.status = "failed"
            report.error = str(e)
            report.finished_at = datetime.utcnow()
            db.commit()
            if run is not None:
                _log(db, run, f"synthesis failed: {e}", "error")
            return "failed"

        if result.get("_transient_error") and run is not None:
            _log(db, run, f"poll transient: {result['_transient_error']}", "warn")

        if result["status"] in ("ready", "failed"):
            _persist_research_terminal(db, report, result)
            if run is not None:
                if result["status"] == "ready":
                    title = (
                        f"Market synthesis · {report.started_at:%Y-%m-%d}"
                    )
                    db.add(RunEvent(
                        run_id=run.id,
                        level="material",
                        message=f"[synthesis] {title} ready",
                        meta={
                            "synthesis_id": report.id,
                            "title": title,
                        },
                    ))
                    db.commit()
                    _log(db, run, "synthesis ready")
                else:
                    _log(
                        db, run,
                        f"synthesis failed: {report.error or 'unknown'}",
                        "error",
                    )
            return result["status"]

        _time.sleep(_MARKET_SYNTHESIS_POLL_S)


def run_market_synthesis_job(
    triggered_by: str = "manual",
    agent: str = "preview",
    window_days: int = 30,
    brief: str | None = None,
) -> None:
    """Background job: compose a cross-competitor brief, kick off a Gemini
    Deep Research interaction, and poll it to completion. Creates a Run
    row so the synthesis shows up on /runs alongside scans/digests.

    Uniform entry point for both the weekly cron (agent='max',
    triggered_by='scheduled') and the manual Run button
    (agent='preview', triggered_by='manual'). Concurrency is gated at the
    route + scheduler layer (at-most-1 synthesis in flight), not here —
    this wrapper assumes the caller already holds the slot.

    When `brief` is provided (operator edited it in the pre-run form),
    that text is sent to Gemini verbatim. We still call compose_brief()
    to capture telemetry (competitors_covered, findings_count, etc.) so
    the detail page's inputs-line renders; any divergence between the
    composed default and the submitted text is flagged with
    `inputs_meta['edited'] = True`.
    """
    from .adapters import gemini_research as _gem
    from .market_synthesis import compose_brief

    run, db = _start_run("market_synthesis", triggered_by)
    token = current_run_id.set(run.id)
    deadline = datetime.utcnow() + timedelta(
        seconds=MARKET_SYNTHESIS_TIMEOUT_S
    )
    report: MarketSynthesisReport | None = None
    try:
        _log(db, run, f"composing brief (window={window_days}d, agent={agent})")
        try:
            default_brief, inputs_meta = compose_brief(db, window_days=window_days)
        except Exception as e:
            _log(db, run, f"brief composition failed: {e}", "error")
            _finish_run(db, run, "error", str(e))
            return

        # Operator-edited brief wins; otherwise use the freshly composed
        # default. Whitespace-only edits fall back to the default so an
        # accidentally cleared textarea doesn't silently send an empty
        # brief to Gemini.
        submitted = (brief or "").strip()
        if submitted:
            final_brief = submitted
            inputs_meta = dict(inputs_meta)
            inputs_meta["edited"] = True
            inputs_meta["brief_chars"] = len(final_brief)
        else:
            final_brief = default_brief

        _log(
            db, run,
            f"brief: {inputs_meta['competitors_covered']} competitors, "
            f"{inputs_meta['findings_count']} findings, "
            f"{inputs_meta['dr_reports_used']} DR excerpts, "
            f"{inputs_meta['brief_chars']} chars"
            + (" (edited)" if inputs_meta.get("edited") else "")
            + (" (truncated)" if inputs_meta.get("truncated") else ""),
        )
        if inputs_meta.get("missing_context"):
            _log(
                db, run,
                "missing ContextBrief scope(s): "
                + ", ".join(inputs_meta["missing_context"]),
                "warn",
            )

        report = MarketSynthesisReport(
            run_id=run.id,
            agent=agent,
            status="queued",
            triggered_by=triggered_by,
            window_days=int(window_days),
            brief=final_brief,
            inputs_meta=inputs_meta,
        )
        db.add(report)
        db.commit()
        db.refresh(report)

        try:
            iid = _gem.start_research(report.brief, agent=agent)
        except _gem.GeminiUnavailable as e:
            report.status = "failed"
            report.error = str(e)
            report.finished_at = datetime.utcnow()
            db.commit()
            _log(db, run, f"synthesis failed: {e}", "error")
            _finish_run(db, run, "error", str(e))
            return

        report.interaction_id = iid
        report.status = "running"
        db.commit()
        _log(db, run, f"interaction started: {iid}")

        final = _poll_synthesis_to_terminal(db, report, run, deadline)
        _finish_run(
            db, run,
            "ok" if final == "ready" else "error",
            error=report.error if final != "ready" else None,
        )
    except Exception as e:
        tb = traceback.format_exc()
        _log(db, run, f"ERROR: {e}\n{tb}", "error")
        # Best-effort: surface the error on the report row if it was created.
        try:
            if report is not None:
                r = db.get(MarketSynthesisReport, report.id)
                if r and r.status not in ("ready", "failed"):
                    r.status = "failed"
                    r.error = f"{type(e).__name__}: {e}"
                    r.finished_at = datetime.utcnow()
                    db.commit()
        except Exception:
            pass
        _finish_run(db, run, "error", str(e))
    finally:
        current_run_id.reset(token)


def run_discover_competitors_job(hint: str | None = None,
                                 triggered_by: str = "manual") -> None:
    """Background job: run the competitor-discovery tool-use loop and
    persist each returned candidate as a CompetitorCandidate row.

    Exclusion list = active competitors ∪ previously dismissed candidates
    (both keyed on homepage_domain). The prompt is told to skip them; we
    also drop anything that slips through at persist time as defence in
    depth.

    Writes RunEvents for progress + a single 'material' event on completion
    so /runs and the live log surface the outcome.
    """
    run, db = _start_run("discover_competitors", triggered_by)
    token = current_run_id.set(run.id)
    try:
        import json as _json
        cfg_path = os.environ.get("CONFIG_PATH", "config.json")
        try:
            with open(cfg_path) as _f:
                config = _json.load(_f)
        except Exception:
            config = {}
        company = config.get("company", "Seek")
        industry = config.get("industry", "job search and recruitment platforms")

        existing_rows = db.query(Competitor).filter(Competitor.active == True).all()  # noqa: E712
        existing = [c.homepage_domain for c in existing_rows if c.homepage_domain]
        dismissed = [
            d[0] for d in (
                db.query(CompetitorCandidate.homepage_domain)
                .filter(CompetitorCandidate.status == "dismissed")
                .filter(CompetitorCandidate.homepage_domain.isnot(None))
                .all()
            )
        ]

        _log(db, run, f"discovering (excluding {len(existing)} tracked · "
                      f"{len(dismissed)} dismissed)")
        if hint:
            _log(db, run, f"focus: {hint[:200]}")

        from .competitor_discover import discover_stream

        exclude = {d for d in (existing + dismissed) if d}
        candidates: list[dict] = []
        with _StreamToRunEvents(run.id) as tee, contextlib.redirect_stdout(tee):
            for event in discover_stream(
                company, industry, existing, dismissed, hint=hint,
            ):
                etype = event.get("type")
                if etype == "progress":
                    print(f"[discover] {event.get('message', '')}")
                elif etype == "error":
                    raise RuntimeError(event.get("message") or "discover failed")
                elif etype == "done":
                    candidates = event.get("candidates") or []

        kept = 0
        for cand in candidates:
            domain = cand.get("homepage_domain")
            if domain and domain in exclude:
                continue
            db.add(CompetitorCandidate(
                run_id=run.id,
                name=cand["name"],
                homepage_domain=domain,
                category=cand.get("category"),
                one_line_why=cand.get("one_line_why") or "",
                evidence=cand.get("evidence") or [],
                status="suggested",
                run_hint=(hint or None),
            ))
            kept += 1
        db.commit()

        msg = f"[discover] {kept} new candidate{'s' if kept != 1 else ''}"
        db.add(RunEvent(
            run_id=run.id,
            level="material",
            message=msg,
            meta={"kind": "discover_competitors", "count": kept, "hint": hint},
        ))
        db.commit()
        _finish_run(db, run, "ok")
    except Exception as e:
        tb = traceback.format_exc()
        _log(db, run, f"ERROR: {e}\n{tb}", "error")
        _finish_run(db, run, "error", str(e))
    finally:
        current_run_id.reset(token)


def resume_in_flight_market_synthesis() -> int:
    """Startup sweep for synthesis rows in queued/running at boot.
    Same semantics as `resume_in_flight_research` (Spec 04): rows with a
    live interaction_id get re-polled; rows without one are marked failed.
    Returns count of rows touched."""
    from .adapters import gemini_research as _gem
    import threading as _threading

    db = SessionLocal()
    touched = 0
    try:
        rows = (
            db.query(MarketSynthesisReport)
            .filter(MarketSynthesisReport.status.in_(["queued", "running"]))
            .all()
        )
        for r in rows:
            touched += 1
            if not r.interaction_id:
                r.status = "failed"
                r.error = "interrupted before Gemini returned an interaction id"
                r.finished_at = datetime.utcnow()
                db.commit()
                continue
            try:
                result = _gem.poll_research(r.interaction_id)
            except _gem.GeminiUnavailable as e:
                r.status = "failed"
                r.error = str(e)
                r.finished_at = datetime.utcnow()
                db.commit()
                continue
            if result["status"] in ("ready", "failed"):
                _persist_research_terminal(db, r, result)
                continue
            r.status = "running"
            db.commit()
            rid = r.id
            _threading.Thread(
                target=_resume_synthesis_poller,
                args=(rid,),
                name=f"synthesis-resume-{rid}",
                daemon=True,
            ).start()
    except Exception as e:
        print(f"  [synthesis-resume] sweep failed: {e}", flush=True)
    finally:
        db.close()
    return touched


def _resume_synthesis_poller(report_id: int) -> None:
    """Thread target: resume polling a synthesis row after a restart.
    No Run row (same rationale as `_resume_research_poller`)."""
    db = SessionLocal()
    try:
        r = db.get(MarketSynthesisReport, report_id)
        if r is None or r.status not in ("queued", "running"):
            return
        deadline = datetime.utcnow() + timedelta(
            seconds=MARKET_SYNTHESIS_TIMEOUT_S
        )
        _poll_synthesis_to_terminal(db, r, None, deadline)
    except Exception as e:
        print(f"  [synthesis-resume] poller {report_id} failed: {e}", flush=True)
    finally:
        db.close()


def run_momentum_job(country: str = "au", triggered_by: str = "schedule"):
    """Collect daily momentum signals (Google Trends, iOS rank, Play installs/
    rating/reviews) for every active competitor. Emits events into the live log
    so the UI shows per-competitor progress the same way the scan does."""
    from .momentum import run_momentum_job as _collect
    run, db = _start_run("momentum", triggered_by)
    token = current_run_id.set(run.id)
    try:
        with _StreamToRunEvents(run.id) as _tee, contextlib.redirect_stdout(_tee):
            summary = _collect(country=country)
        ok_count = len(summary.get("competitors", []))
        err_count = len(summary.get("errors", []))
        _log(db, run, f"momentum: {ok_count} competitors collected, {err_count} errors")
        _finish_run(db, run, "ok")
    except Exception as e:
        tb = traceback.format_exc()
        _log(db, run, f"ERROR: {e}\n{tb}", "error")
        _finish_run(db, run, "error", str(e))
    finally:
        current_run_id.reset(token)


def run_ingest_app_reviews_job(triggered_by: str = "schedule",
                               run_id: int | None = None):
    """Daily ingest pass: pull latest reviews from every enabled
    AppReviewSource and upsert into app_reviews. Zero LLM cost; the
    synthesis pass (run_voc_themes_job) is what reads the corpus.
    Spec: docs/voc/01-app-reviews.md."""
    from .app_reviews import ingest_all
    run, db = _start_run("ingest_app_reviews", triggered_by, run_id=run_id)
    token = current_run_id.set(run.id)
    try:
        from service import load_config
        try:
            config = load_config()
        except Exception:
            config = {}
        with _StreamToRunEvents(run.id) as _tee, contextlib.redirect_stdout(_tee):
            print("[app-reviews] ingest sweep")
            result = ingest_all(db, config=config)
            print(
                f"[app-reviews] sources={result.sources_processed} "
                f"failed={result.sources_failed} "
                f"inserted={result.rows_inserted} "
                f"skipped_dedup={result.rows_skipped_dedup}"
            )
        db.add(RunEvent(
            run_id=run.id,
            level="material",
            message=(
                f"[material] app-reviews ingest · "
                f"{result.sources_processed} sources · "
                f"{result.rows_inserted} new reviews · "
                f"{result.rows_skipped_dedup} dedup skips"
            ),
            meta={
                "sources_processed": result.sources_processed,
                "sources_failed": result.sources_failed,
                "rows_inserted": result.rows_inserted,
                "rows_skipped_dedup": result.rows_skipped_dedup,
            },
        ))
        db.commit()
        _finish_run(db, run, "ok")
    except Exception as e:
        tb = traceback.format_exc()
        _log(db, run, f"ERROR: {e}\n{tb}", "error")
        _finish_run(db, run, "error", str(e))
    finally:
        current_run_id.reset(token)


def run_voc_themes_job(competitor_id: int | None = None,
                       triggered_by: str = "schedule",
                       force: bool = False,
                       run_id: int | None = None):
    """Weekly synthesis pass: for each eligible competitor, take the recent
    review corpus + current themes and ask Haiku for an updated theme set
    plus a per-theme diff. Findings emit only on emergence or material shift.

    competitor_id=None  → sweep every active competitor with ≥1 enabled source.
    competitor_id=N     → run for one competitor (admin manual trigger).
    force=True          → bypass the ≥10-reviews-in-60d guard (manual only).
    Spec: docs/voc/01-app-reviews.md."""
    from .voc_themes import synthesise_all, synthesise_for_competitor
    from .models import Competitor
    run, db = _start_run("synthesise_voc_themes", triggered_by, run_id=run_id)
    token = current_run_id.set(run.id)
    try:
        from service import load_config
        try:
            config = load_config()
        except Exception:
            config = {}

        with _StreamToRunEvents(run.id) as _tee, contextlib.redirect_stdout(_tee):
            if competitor_id is not None:
                comp = db.get(Competitor, competitor_id)
                if comp is None:
                    raise ValueError(f"competitor {competitor_id} not found")
                print(f"[voc-themes] synthesise for {comp.name!r} (force={force})")
                per = synthesise_for_competitor(
                    db, comp, config=config, run_id=run.id, force=force,
                )
                if per.skipped_reason:
                    print(f"[voc-themes] skipped: {per.skipped_reason}")
                else:
                    print(
                        f"[voc-themes] {comp.name}: themes_total={per.themes_total} "
                        f"new={per.new} shifted={per.shifted} dropped={per.dropped} "
                        f"findings={per.findings_emitted}"
                    )
                db.add(RunEvent(
                    run_id=run.id,
                    level="material",
                    message=(
                        f"[material] voc-themes · {comp.name} · "
                        f"{per.themes_total} themes · "
                        f"{per.findings_emitted} findings emitted"
                    ),
                    meta={
                        "competitor_id": comp.id,
                        "competitor_name": comp.name,
                        "themes_total": per.themes_total,
                        "new": per.new,
                        "shifted": per.shifted,
                        "dropped": per.dropped,
                        "findings_emitted": per.findings_emitted,
                        "skipped_reason": per.skipped_reason,
                    },
                ))
            else:
                print("[voc-themes] sweep all eligible competitors")
                sweep = synthesise_all(db, config=config, run_id=run.id)
                print(
                    f"[voc-themes] processed={sweep.competitors_processed} "
                    f"skipped={sweep.competitors_skipped} "
                    f"findings_emitted={sweep.findings_emitted}"
                )
                for per in sweep.per_competitor:
                    if per.skipped_reason:
                        print(
                            f"[voc-themes] - {per.competitor_name}: "
                            f"skipped ({per.skipped_reason})"
                        )
                        continue
                    print(
                        f"[voc-themes] - {per.competitor_name}: "
                        f"themes={per.themes_total} new={per.new} "
                        f"shifted={per.shifted} dropped={per.dropped} "
                        f"findings={per.findings_emitted}"
                    )
                db.add(RunEvent(
                    run_id=run.id,
                    level="material",
                    message=(
                        f"[material] voc-themes sweep · "
                        f"{sweep.competitors_processed} competitors · "
                        f"{sweep.findings_emitted} findings emitted"
                    ),
                    meta={
                        "competitors_processed": sweep.competitors_processed,
                        "competitors_skipped": sweep.competitors_skipped,
                        "findings_emitted": sweep.findings_emitted,
                    },
                ))
            db.commit()
        _finish_run(db, run, "ok")
    except Exception as e:
        tb = traceback.format_exc()
        _log(db, run, f"ERROR: {e}\n{tb}", "error")
        _finish_run(db, run, "error", str(e))
    finally:
        current_run_id.reset(token)
