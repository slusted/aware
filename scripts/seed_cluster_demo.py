"""One-shot seed for a visual smoke test of spec 06 clustering + MMR.

Creates an admin user + auth session (prints the cookie value), and
inserts a handful of findings designed to exercise both features:

  - Two near-duplicate "Indeed raises Series D" funding findings → cluster of 2.
  - Three Indeed × new_hire stories clustered into one, plus two more
    same-dim cards → lets MMR break up the monotony.
  - A LinkedIn new_hire and an Indeed product_launch → dissimilar neighbors
    MMR should lift into top slots.

Run: python scripts/seed_cluster_demo.py
"""
from __future__ import annotations

import hashlib
import secrets
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.orm import Session

from app import models
from app.db import engine


def mkhash(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"|")
    return h.hexdigest()[:32]


def upsert_finding(
    s: Session,
    *,
    competitor: str,
    title: str,
    signal_type: str,
    source: str = "news",
    materiality: float = 0.5,
    topic: str | None = None,
    age_days: float = 0.0,
) -> models.Finding:
    h = mkhash(competitor, title, signal_type, source, str(age_days))
    existing = s.query(models.Finding).filter(models.Finding.hash == h).first()
    if existing:
        return existing
    f = models.Finding(
        competitor=competitor,
        title=title,
        signal_type=signal_type,
        source=source,
        topic=topic,
        materiality=materiality,
        hash=h,
        created_at=datetime.utcnow() - timedelta(days=age_days),
        summary=title,  # so the card body isn't empty
    )
    s.add(f)
    s.flush()
    return f


def main() -> None:
    with Session(engine) as s:
        # Admin user + session cookie.
        user = s.query(models.User).filter(models.User.email == "demo@local").first()
        if user is None:
            user = models.User(
                email="demo@local",
                name="Demo",
                role="admin",
                password_hash=None,  # we don't need to log in via form
                is_active=True,
            )
            s.add(user)
            s.flush()

        token = secrets.token_hex(16)
        s.add(models.AuthSession(
            token=token,
            user_id=user.id,
            expires_at=datetime.utcnow() + timedelta(days=7),
        ))
        s.commit()

        print(f"SESSION_TOKEN={token}")
        print(f"USER_ID={user.id}")

        # Ensure competitors exist (logo lookups are non-fatal if missing).
        for name in ("Indeed", "LinkedIn"):
            c = s.query(models.Competitor).filter(models.Competitor.name == name).first()
            if c is None:
                s.add(models.Competitor(name=name, source="manual", active=True))
        s.commit()

        # Findings — the blend that makes clustering + MMR observable.
        findings_plan = [
            # Two near-dupe funding stories → cluster of 2.
            dict(competitor="Indeed", signal_type="funding",
                 title="Indeed raises $100m Series D led by Sequoia",
                 source="techcrunch", materiality=0.9, age_days=1),
            dict(competitor="Indeed", signal_type="funding",
                 title="Indeed raises $100m in Series D round",
                 source="reuters", materiality=0.7, age_days=1),

            # Three near-dupe new_hire stories → cluster of 3.
            dict(competitor="Indeed", signal_type="new_hire",
                 title="Indeed hires Jane Doe as new Chief Revenue Officer",
                 source="news", materiality=0.7, age_days=0.5),
            dict(competitor="Indeed", signal_type="new_hire",
                 title="Indeed names new Chief Revenue Officer Jane Doe",
                 source="news", materiality=0.6, age_days=0.5),
            dict(competitor="Indeed", signal_type="new_hire",
                 title="New Indeed CRO Jane Doe announced",
                 source="news", materiality=0.5, age_days=0.5),

            # Standalone dissimilar cards — MMR should surface these high.
            dict(competitor="LinkedIn", signal_type="new_hire",
                 title="LinkedIn hires new VP Engineering",
                 source="techcrunch", materiality=0.55, age_days=2),
            dict(competitor="Indeed", signal_type="product_launch",
                 title="Indeed launches Resume Assistant AI tool",
                 source="news", materiality=0.65, age_days=3),
            dict(competitor="LinkedIn", signal_type="product_launch",
                 title="LinkedIn opens new learning hub",
                 source="news", materiality=0.45, age_days=4),
            dict(competitor="Indeed", signal_type="messaging_shift",
                 title="Indeed pivots messaging toward SMB employers",
                 source="news", materiality=0.5, age_days=5),
        ]
        for plan in findings_plan:
            upsert_finding(s, **plan)
        s.commit()
        print(f"FINDINGS_SEEDED={len(findings_plan)}")


if __name__ == "__main__":
    main()
