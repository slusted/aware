"""Usage tracking: instruments Claude + Tavily calls and writes to the
`usage_events` table. Works by monkey-patching the clients at startup so the
existing scanner.py / analyzer.py / competitor_manager.py / doc_processor.py
modules don't need changes (beyond a small hook in search_tavily).

run_id attribution: jobs.py sets `current_run_id` at the start of each job;
any API call made inside the job inherits that context.
"""
import contextvars
import traceback

from .db import SessionLocal
from .models import UsageEvent
from . import pricing

current_run_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "current_run_id", default=None
)

_installed = False


def record_claude(model: str, usage, operation: str = "messages.create",
                  run_id: int | None = None):
    """Insert one row per Claude API call. Safe to fail — swallows errors so a
    logging hiccup never breaks a scan."""
    try:
        it = getattr(usage, "input_tokens", 0) or 0
        ot = getattr(usage, "output_tokens", 0) or 0
        cr = getattr(usage, "cache_read_input_tokens", 0) or 0
        cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cost = pricing.claude_cost(model, it, ot, cr, cw)
        db = SessionLocal()
        try:
            db.add(UsageEvent(
                run_id=run_id if run_id is not None else current_run_id.get(),
                provider="claude",
                operation=operation,
                model=model,
                input_tokens=it,
                output_tokens=ot,
                cache_read_tokens=cr,
                cache_write_tokens=cw,
                cost_usd=cost,
                success=True,
            ))
            db.commit()
        finally:
            db.close()
    except Exception:
        traceback.print_exc()


def record_tavily(depth: str = "advanced", topic: str = "general",
                  result_count: int = 0, success: bool = True,
                  run_id: int | None = None):
    try:
        credits, cost = pricing.tavily_cost(depth) if success else (0, 0.0)
        db = SessionLocal()
        try:
            db.add(UsageEvent(
                run_id=run_id if run_id is not None else current_run_id.get(),
                provider="tavily",
                operation="search",
                model=depth,
                credits=credits,
                cost_usd=cost,
                success=success,
                extra={"topic": topic, "result_count": result_count},
            ))
            db.commit()
        finally:
            db.close()
    except Exception:
        traceback.print_exc()


def _wrap_anthropic_client(client):
    """Wrap a single anthropic.Anthropic instance so every messages.create
    call is recorded. Idempotent: re-wrapping is a no-op."""
    if getattr(client.messages, "_cw_wrapped", False):
        return
    original = client.messages.create

    def wrapped(*args, **kwargs):
        resp = original(*args, **kwargs)
        try:
            record_claude(
                model=kwargs.get("model", "unknown"),
                usage=getattr(resp, "usage", None),
            )
        except Exception:
            traceback.print_exc()
        return resp

    wrapped._cw_wrapped = True  # type: ignore[attr-defined]
    client.messages.create = wrapped  # type: ignore[method-assign]
    client.messages._cw_wrapped = True  # type: ignore[attr-defined]


def install_hooks():
    """Call once at app startup. Imports the engine modules and wraps their
    module-level Anthropic clients + the Tavily search function."""
    global _installed
    if _installed:
        return
    try:
        import analyzer
        _wrap_anthropic_client(analyzer.client)
    except Exception:
        traceback.print_exc()
    try:
        import competitor_manager
        _wrap_anthropic_client(competitor_manager.client)
    except Exception:
        traceback.print_exc()
    try:
        import doc_processor
        # doc_processor instantiates client inside functions; see note below.
        # No-op here; if hot paths are called we'll patch there instead.
        pass
    except Exception:
        pass
    _installed = True
