"""Thin wrappers around the existing scanner/analyzer/service modules.
Each job writes Run + RunEvent + Finding/Report rows so the UI has visibility.
The heavy lifting stays in the original modules — this layer just logs to DB.
"""
import contextlib
import os
import sys
import threading
import traceback
from datetime import datetime

from .db import SessionLocal
from .models import Run, RunEvent, Finding, Report
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


def _start_run(kind: str, triggered_by: str = "schedule") -> tuple[Run, object]:
    db = SessionLocal()
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

    try:
        # 1. Web search (blocks on Tavily HTTP; this is why we parallelize).
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
        # signal_type + materiality + summary together.
        enriched: dict[str, tuple[str, float, dict, str | None]] = {}
        if to_enrich:
            from concurrent.futures import ThreadPoolExecutor
            def _one(item):
                f, h = item
                llm = _llm_cs(f, name)
                if llm is not None:
                    return h, (llm["signal_type"], llm["materiality"],
                               llm["payload"], llm["summary"])
                # Fallback to deterministic regex classifier; no summary.
                st, mat, payload = _classify(f)
                return h, (st, mat, payload, None)
            with ThreadPoolExecutor(max_workers=min(8, len(to_enrich))) as pool:
                for h, tup in pool.map(_one, to_enrich):
                    enriched[h] = tup

        to_save: list[tuple[dict, str, str, float, dict]] = []
        summaries: dict[str, str | None] = {}
        for f, h in to_enrich:
            st, mat, payload, summary = enriched[h]
            to_save.append((f, h, st, mat, payload))
            summaries[h] = summary

        for f, h, st, mat, payload in to_save:
            db.add(FindingModel(
                run_id=run_id,
                competitor=name,
                source=f.get("source", ""),
                topic=f.get("topic"),
                title=f.get("title"),
                url=f.get("url"),
                content=f.get("content") or "",
                summary=summaries.get(h),
                hash=h,
                search_provider=f.get("search_provider"),
                score=_coerce_score(f.get("relevance")),
                published_at=_parse_published(f.get("published")),
                signal_type=st,
                materiality=mat,
                payload=payload,
                matched_keyword=f.get("matched_keyword"),
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


def run_scan_job(triggered_by: str = "schedule", freshness_days: int | None = None):
    """Per-competitor pipeline, then market summary:
      1. For each competitor (in parallel): scan → save findings → synthesize review
      2. Stuff fresh reviews into memory so the market digest has synthesized context
      3. Market summary (one Claude call)
      4. Save Report, email team

    Competitor pages go live as each worker completes; the market digest is the
    final synthesis across all of them.
    """
    run, db = _start_run("scan", triggered_by)
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


def run_market_digest_job(triggered_by: str = "manual"):
    """Regenerate the market digest from existing findings — no new scraping.

    Uses the most recent completed scan's findings if available, otherwise
    falls back to the last 14 days across all runs. Seeds memory with the
    current competitor reviews + context briefs so the digest reads the same
    synthesized view a scan would have. Writes a Report row and logs progress
    as a Run with kind='market_digest' so the live panel + /runs show it.
    """
    run, db = _start_run("market_digest", triggered_by)
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


def run_discovery_job():
    run, db = _start_run("discovery", "manual")
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
