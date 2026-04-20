"""
Competitor Watch Service
========================
The main entry point. Runs continuously:
  - Daily scan at configured hour
  - Checks for email replies every few minutes
  - Handles follow-ups from team members

Environment variables:
  ANTHROPIC_API_KEY  (required) — Claude API key
  TAVILY_API_KEY     (required) — Tavily search API key (get free at tavily.com)
  GMAIL_USER         (optional) — Gmail address for sending digests
  GMAIL_APP_PASSWORD (optional) — Gmail app password for SMTP/IMAP

Deploy to Railway, or run locally:
  python service.py
"""

import os
import sys
import json
import time
import traceback
from datetime import datetime

# Load .env if present (optional — env vars from the shell still take precedence
# because load_dotenv does not override existing variables by default)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed — rely on shell env vars only

# Validate environment
required = ["ANTHROPIC_API_KEY"]
missing = [v for v in required if not os.environ.get(v)]
if missing:
    print("Missing required environment variables:")
    for v in missing:
        print(f"  {v}")
    print("\nOptional (for email):")
    print("  GMAIL_USER, GMAIL_APP_PASSWORD")
    sys.exit(1)

from scanner import run_full_scan, load_memory, save_memory
from analyzer import analyze_findings, handle_follow_up
from deepen import deepen_findings, render_trace_markdown
from mailer import send_digest_to_team, send_email, check_for_replies, SUBJECT_PREFIX
from doc_processor import process_docs, discover_docs
from competitor_manager import run_discovery, prune_competitors, record_scan_activity, get_watchlist_status


def load_config() -> dict:
    config_path = os.environ.get("CONFIG_PATH",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


def _last_report_date():
    """Return the date of the most recent report file, or None if none exist.
    Used to skip the startup scan when the service restarts after a scan already ran today.
    Report filenames follow: report_YYYYMMDD_HHMMSS.md
    """
    reports_dir = os.path.join(os.environ.get("DATA_DIR", "data"), "reports")
    if not os.path.isdir(reports_dir):
        return None
    latest = None
    for fname in os.listdir(reports_dir):
        if not fname.startswith("report_") or not fname.endswith(".md"):
            continue
        try:
            stamp = fname[len("report_"):-len(".md")]
            d = datetime.strptime(stamp, "%Y%m%d_%H%M%S").date()
            if latest is None or d > latest:
                latest = d
        except ValueError:
            continue
    return latest


def build_digest(findings: list[dict], analysis: str, memory: dict, deepen_trace: dict | None = None) -> str:
    """Build the full digest text."""
    now = datetime.now()
    digest = f"# Competitor Watch — {now.strftime('%A, %B %d, %Y')}\n\n"
    digest += f"**Run #{memory.get('run_count', 0)}** | {len(findings)} new items found\n\n"
    digest += "---\n\n## Analysis\n\n"
    digest += analysis

    # Agent activity — audit trail of the deepen pass. Rendered always (even
    # on no-op runs) so the reader can see whether the agentic layer ran and
    # what it cost.
    if deepen_trace is not None:
        digest += "\n\n---\n\n" + render_trace_markdown(deepen_trace)

    # Watchlist status (new/pruned competitors, stale warnings)
    try:
        config = load_config()
        watchlist_status = get_watchlist_status(config)
        if watchlist_status:
            digest += f"\n\n---\n\n## Watchlist Changes\n\n{watchlist_status}\n"
    except Exception:
        pass

    digest += f"\n\n---\n\n## Raw Findings ({len(findings)} items)\n\n"

    by_comp = {}
    for f in findings:
        by_comp.setdefault(f["competitor"], []).append(f)

    for comp, items in by_comp.items():
        digest += f"\n### {comp}\n\n"
        for item in items[:10]:
            title = item.get('title', '')
            title_part = f" {title} —" if title else ""
            digest += f"- **[{item['source']}/{item['topic']}]**{title_part} {item['content'][:200]}\n"
            # Surface the deepen-pass rationale so the reader can see WHY
            # the model chose to promote this item into the digest.
            rationale = item.get("rationale")
            if rationale:
                digest += f"  - _why surfaced:_ {rationale}\n"

    if not findings:
        digest += "\nNo new findings today.\n"

    return digest


def run_daily_scan(config: dict):
    """Execute one full scan cycle."""
    print(f"\n{'='*55}")
    print(f"  Daily Scan — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*55}")

    # Run competitor discovery + pruning (weekly, not every scan)
    try:
        discovery_result = run_discovery(config)
        prune_result = prune_competitors(config)
        # Reload config if competitors were added/removed
        if discovery_result.get("added") or prune_result.get("pruned"):
            config = load_config()
    except Exception as e:
        print(f"  [discovery/prune] Error (non-fatal): {e}")

    findings, memory = run_full_scan(config)

    # Agentic follow-up pass — Claude picks 1–2 loose threads from the
    # scripted findings and chases them with a small search budget. Fails
    # soft: on any error the original findings pass through unchanged.
    # Tunable via DEEPEN_ENABLED / DEEPEN_MAX_SEARCHES / DEEPEN_MAX_NEW_FINDINGS.
    deepen_trace = None
    try:
        findings, deepen_trace = deepen_findings(findings, config, memory)
        save_memory(memory)  # persist any seen-hashes added during deepen
    except Exception as e:
        print(f"  [deepen] Unhandled error (non-fatal): {e}")

    # Track activity for pruning decisions
    try:
        record_scan_activity(findings, config)
    except Exception as e:
        print(f"  [activity] Tracking error (non-fatal): {e}")

    analysis = analyze_findings(findings, config, memory)
    digest = build_digest(findings, analysis, memory, deepen_trace=deepen_trace)

    # Save report locally
    reports_dir = os.path.join(os.environ.get("DATA_DIR", "data"), "reports")
    os.makedirs(reports_dir, exist_ok=True)
    report_path = os.path.join(reports_dir, f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(digest)
    print(f"  [report] Saved to {report_path}")

    # Email to team
    send_digest_to_team(digest, config, findings)

    return digest


def process_replies(config: dict):
    """Check for and handle team replies."""
    replies = check_for_replies(config)
    if not replies:
        return

    memory = load_memory()
    for reply in replies:
        print(f"\n  [reply] From {reply['from_name'] or reply['from']}: {reply['body'][:60]}...")

        try:
            answer = handle_follow_up(reply["body"], config, memory)
            subject = f"Re: {reply['subject'].replace('Re: ', '').replace('RE: ', '')}"
            body = f"# Follow-up: {reply['body'][:80]}\n\n{answer}"
            body += "\n\n---\n_Reply again to ask more questions._"
            send_email(reply["from"], subject, body)
        except Exception as e:
            print(f"  [reply] Error handling reply: {e}")
            traceback.print_exc()


def main():
    config = load_config()
    scan_hour = config.get("scan_hour", 8)
    reply_mins = config.get("reply_check_minutes", 5)

    comp_names = ", ".join(c["name"] for c in config["competitors"])
    team_names = ", ".join(m["name"] for m in config["team"])

    print("=" * 55)
    print("  Competitor Watch Service")
    print(f"  Company:      {config['company']}")
    print(f"  Competitors:  {comp_names}")
    print(f"  Team:         {team_names}")
    print(f"  Daily scan:   {scan_hour}:00")
    print(f"  Reply check:  every {reply_mins} min")
    print("=" * 55)

    last_scan_date = _last_report_date()
    last_reply_check = datetime.min

    # Process strategy documents on startup
    print("\n  Processing strategy documents...")
    try:
        docs = discover_docs()
        seek_count = len(docs["seek"])
        comp_count = sum(len(v) for v in docs["competitors"].values())
        if seek_count or comp_count:
            print(f"  [docs] Found {seek_count} Seek docs, {comp_count} competitor docs")
            process_docs()
        else:
            print("  [docs] No documents found — add files to docs/seek/ and docs/competitors/")
    except Exception as e:
        print(f"  [docs] Document processing failed (non-fatal): {e}")

    # Initial scan on startup — skip if a scan already ran today (e.g. service restarted)
    today = datetime.now().date()
    if last_scan_date == today:
        print(f"\n  Skipping initial scan — already ran today (last report: {last_scan_date})")
    else:
        print("\n  Running initial scan...")
        try:
            run_daily_scan(config)
            last_scan_date = datetime.now().date()
        except Exception as e:
            print(f"  [error] Initial scan failed: {e}")
            traceback.print_exc()

    # Main loop
    while True:
        try:
            now = datetime.now()

            # Daily scan
            if now.date() != last_scan_date and now.hour >= scan_hour:
                try:
                    run_daily_scan(config)
                    last_scan_date = now.date()
                except Exception as e:
                    print(f"  [error] Scan failed: {e}")
                    traceback.print_exc()

            # Reply check
            if (now - last_reply_check).total_seconds() >= reply_mins * 60:
                try:
                    process_replies(config)
                except Exception as e:
                    print(f"  [error] Reply check failed: {e}")
                last_reply_check = now

            time.sleep(30)

        except KeyboardInterrupt:
            print("\n\n  Shutting down.")
            break
        except Exception as e:
            print(f"\n  [error] {e}")
            time.sleep(60)


if __name__ == "__main__":
    main()
