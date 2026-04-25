"""End-to-end verification for semantic ranking
(docs/ranker/08-semantic-ranking.md).

Hermetic — no Voyage calls. We mock `embed_documents` so the centroid
math runs against known vectors, then check the scorer's embedding term
behaves as specified.

Usage:
    python scripts/verify_semantic_ranking.py
Exit code: 0 on all pass, 1 on any fail.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="sem_rank_verify_")) / "verify.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
from sqlalchemy import create_engine, inspect  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db import Base  # noqa: E402
from app import models  # noqa: E402
from app.adapters import voyage as _voyage  # noqa: E402
from app.ranker import config as rcfg  # noqa: E402
from app.ranker.preferences import load_profile  # noqa: E402
from app.ranker.present import default_score  # noqa: E402
from app.ranker.rollup import rebuild_user_preferences  # noqa: E402


# ── Harness ─────────────────────────────────────────────────────────

_passes = 0
_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global _passes
    status = "PASS" if ok else "FAIL"
    line = f"  [{status}] {name}"
    if detail:
        line += f"  — {detail}"
    print(line)
    if ok:
        _passes += 1
    else:
        _failures.append(name)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


engine = create_engine(os.environ["DATABASE_URL"])
Base.metadata.create_all(bind=engine)


# Three mutually-orthogonal directions — easy to reason about cosines.
DIM = rcfg.EMBEDDING_DIM
E_HIRE = np.zeros(DIM, dtype=np.float32); E_HIRE[0] = 1.0
E_LAUNCH = np.zeros(DIM, dtype=np.float32); E_LAUNCH[1] = 1.0
E_PRICE = np.zeros(DIM, dtype=np.float32); E_PRICE[2] = 1.0


with Session(engine) as s:
    u = models.User(email="t@t", name="t", role="admin", is_active=True)
    s.add(u)
    s.commit()
    s.refresh(u)
    USER_ID = u.id

    # Three findings, each pre-stamped with a known orthogonal embedding.
    f_hire = models.Finding(
        competitor="Indeed", source="news", signal_type="new_hire",
        topic="talent", matched_keyword="hiring", hash="h1",
        embedding=_voyage.pack(E_HIRE),
        embedding_model=rcfg.EMBEDDING_MODEL,
    )
    f_launch = models.Finding(
        competitor="Indeed", source="news", signal_type="product_launch",
        topic="platform", matched_keyword="launch", hash="h2",
        embedding=_voyage.pack(E_LAUNCH),
        embedding_model=rcfg.EMBEDDING_MODEL,
    )
    f_price = models.Finding(
        competitor="LinkedIn", source="news", signal_type="price_change",
        topic="pricing", matched_keyword="price", hash="h3",
        embedding=_voyage.pack(E_PRICE),
        embedding_model=rcfg.EMBEDDING_MODEL,
    )
    # One finding without an embedding to verify graceful skip.
    f_unembedded = models.Finding(
        competitor="LinkedIn", source="news", signal_type="news",
        hash="h4",
    )
    # One finding with a stale model — must NOT contribute to the centroid
    # and must NOT receive an embedding term in the scorer.
    f_stale = models.Finding(
        competitor="Indeed", source="news", signal_type="new_hire",
        hash="h5",
        embedding=_voyage.pack(E_HIRE),
        embedding_model="some-old-model",
    )
    for f in (f_hire, f_launch, f_price, f_unembedded, f_stale):
        s.add(f)
    s.commit()
    FIDS = {
        "hire": f_hire.id,
        "launch": f_launch.id,
        "price": f_price.id,
        "unembedded": f_unembedded.id,
        "stale": f_stale.id,
    }


def seed_event(event_type: str, finding_id: int | None,
               days_ago: float = 0.0) -> None:
    with Session(engine) as s:
        s.add(models.UserSignalEvent(
            user_id=USER_ID, finding_id=finding_id, event_type=event_type,
            source="stream", value=None, meta={},
            ts=datetime.utcnow() - timedelta(days=days_ago),
        ))
        s.commit()


# ── §1: Pack/unpack round-trip ──────────────────────────────────────
section("Pack/unpack round-trip")

original = np.random.RandomState(7).rand(DIM).astype(np.float32)
roundtripped = _voyage.unpack(_voyage.pack(original))
check("pack->unpack preserves float32 vector",
      roundtripped is not None
      and np.allclose(roundtripped, original, atol=1e-6))

check("unpack(None) returns None", _voyage.unpack(None) is None)
check("unpack on wrong-size blob returns None",
      _voyage.unpack(b"\x00\x00\x00\x00") is None)


# ── §2: Schema shape ────────────────────────────────────────────────
section("Schema shape (spec 08 columns)")

insp = inspect(engine)
finding_cols = {c["name"] for c in insp.get_columns("findings")}
check("findings.embedding exists", "embedding" in finding_cols)
check("findings.embedding_model exists", "embedding_model" in finding_cols)

profile_cols = {c["name"] for c in insp.get_columns("user_preference_profile")}
for col in ("taste_embedding", "taste_embedding_count",
            "taste_embedding_model", "taste_embedding_updated_at"):
    check(f"user_preference_profile.{col} exists", col in profile_cols)


# ── §3: Centroid math (positive only) ───────────────────────────────
section("Centroid: positive-only signals")

# Pin the hire today (+0.80) -> centroid should be exactly E_HIRE
# after L2-normalize (single positive contribution).
seed_event("pin", FIDS["hire"], days_ago=0)

with Session(engine) as s:
    summary = rebuild_user_preferences(s, USER_ID)

check("rollup reports >0 centroid contributions when an embedded finding has +ve event",
      summary.get("centroid_contributions", 0) >= 1,
      f"got {summary.get('centroid_contributions')}")

profile = load_profile(Session(engine), USER_ID)
centroid = profile.taste_embedding
check("centroid is non-None after a positive event on embedded finding",
      centroid is not None)
if centroid is not None:
    check("centroid is L2-normalized (norm ~= 1)",
          abs(float(np.linalg.norm(centroid)) - 1.0) < 1e-5)
    check("single-pin centroid points exactly along E_HIRE",
          float((centroid * E_HIRE).sum()) > 0.999)


# ── §4: Centroid math (signed: positives + negatives) ───────────────
section("Centroid: signed contributions")

# Add a dismiss on the launch finding (−0.70). Centroid should now be a
# unit-length vector pointing along (+0.80 * E_HIRE − 0.70 * E_LAUNCH).
seed_event("dismiss", FIDS["launch"], days_ago=0)

with Session(engine) as s:
    rebuild_user_preferences(s, USER_ID)
profile = load_profile(Session(engine), USER_ID)
centroid = profile.taste_embedding
check("centroid still non-None with a dismiss in the mix", centroid is not None)
if centroid is not None:
    expected_dir = (0.80 * E_HIRE - 0.70 * E_LAUNCH)
    expected_dir = expected_dir / np.linalg.norm(expected_dir)
    cos = float((centroid * expected_dir).sum())
    check("centroid direction matches signed-weighted analytic answer (cos ~= 1)",
          cos > 0.9999, f"cos={cos:.6f}")
    # The dismissed direction (E_LAUNCH) must have NEGATIVE projection on the centroid.
    proj_launch = float((centroid * E_LAUNCH).sum())
    check("dismissed direction has negative projection on centroid",
          proj_launch < 0, f"proj={proj_launch:.4f}")


# ── §5: Pure-negative log still produces a useful centroid ──────────
section("Centroid: pure-negative log")

with Session(engine) as s:
    u3 = models.User(email="neg@t", name="neg", role="admin", is_active=True)
    s.add(u3)
    s.commit()
    s.refresh(u3)
    NEG_USER = u3.id

with Session(engine) as s:
    s.add(models.UserSignalEvent(
        user_id=NEG_USER, finding_id=FIDS["price"], event_type="dismiss",
        source="stream", ts=datetime.utcnow(),
    ))
    s.commit()
    rebuild_user_preferences(s, NEG_USER)

neg_profile = load_profile(Session(engine), NEG_USER)
check("pure-negative log produces non-NULL centroid",
      neg_profile.taste_embedding is not None)
if neg_profile.taste_embedding is not None:
    proj = float((neg_profile.taste_embedding * E_PRICE).sum())
    check("pure-negative centroid points away from dismissed content (proj < 0)",
          proj < 0, f"proj={proj:.4f}")


# ── §6: Stale embedding_model is skipped ────────────────────────────
section("Stale embedding_model handling")

with Session(engine) as s:
    u4 = models.User(email="stale@t", name="stale", role="admin", is_active=True)
    s.add(u4)
    s.commit()
    s.refresh(u4)
    STALE_USER = u4.id

with Session(engine) as s:
    s.add(models.UserSignalEvent(
        user_id=STALE_USER, finding_id=FIDS["stale"], event_type="pin",
        source="stream", ts=datetime.utcnow(),
    ))
    s.commit()
    summary = rebuild_user_preferences(s, STALE_USER)

check("stale-model finding contributes 0 to centroid",
      summary.get("centroid_contributions", 0) == 0,
      f"got {summary.get('centroid_contributions')}")
stale_profile = load_profile(Session(engine), STALE_USER)
check("centroid is None when only stale-model findings are engaged",
      stale_profile.taste_embedding is None)


# ── §7: Scorer math ─────────────────────────────────────────────────
section("Scorer: embedding_match term")

NOW = datetime.utcnow()

# Build a synthetic finding with the SAME direction as the centroid
# (E_HIRE for the §3 user). Scoring should add EMBEDDING_WEIGHT * 1.0.
profile = load_profile(Session(engine), USER_ID)
centroid = profile.taste_embedding

f_aligned = models.Finding(
    competitor="X", source="news", signal_type="new_hire",
    materiality=0.0, hash="z1",
    embedding=_voyage.pack(E_HIRE),
    embedding_model=rcfg.EMBEDDING_MODEL,
    created_at=NOW,
)
f_orth = models.Finding(
    competitor="X", source="news", signal_type="price_change",
    materiality=0.0, hash="z2",
    embedding=_voyage.pack(E_PRICE),
    embedding_model=rcfg.EMBEDDING_MODEL,
    created_at=NOW,
)
f_no_emb = models.Finding(
    competitor="X", source="news", signal_type="news",
    materiality=0.0, hash="z3", created_at=NOW,
)

s_baseline = default_score(f_no_emb, now=NOW, user_centroid=None)
s_with_centroid_no_emb = default_score(f_no_emb, now=NOW, user_centroid=centroid)
check("missing finding embedding -> no embedding term contribution",
      abs(s_baseline - s_with_centroid_no_emb) < 1e-9)

s_no_centroid = default_score(f_aligned, now=NOW, user_centroid=None)
s_centroid_aligned = default_score(f_aligned, now=NOW, user_centroid=centroid)
delta_aligned = s_centroid_aligned - s_no_centroid
# Centroid is the §4 signed centroid — projection on E_HIRE is
# 0.80/sqrt(0.80²+0.70²) ~= 0.7525.
expected_proj = 0.80 / float(np.sqrt(0.80**2 + 0.70**2))
expected_delta = rcfg.EMBEDDING_WEIGHT * expected_proj
check("aligned finding picks up EMBEDDING_WEIGHT * cosine",
      abs(delta_aligned - expected_delta) < 1e-4,
      f"got d={delta_aligned:.4f} expected~={expected_delta:.4f}")

s_centroid_orth = default_score(f_orth, now=NOW, user_centroid=centroid)
delta_orth = s_centroid_orth - default_score(f_orth, now=NOW, user_centroid=None)
# E_PRICE is orthogonal to both E_HIRE and E_LAUNCH, so cos = 0.
check("orthogonal finding picks up ~0 from embedding term",
      abs(delta_orth) < 1e-4, f"got d={delta_orth:.6f}")


# ── §8: Adapter-level: unpack rejects wrong-byte-length blobs ───────
section("Adapter: defensive unpack")

# Half the expected bytes — represents a stale row from a smaller-dim model.
short = b"\x00" * (rcfg.EMBEDDING_DIM * 2)
check("unpack rejects wrong-length blob (returns None, not crash)",
      _voyage.unpack(short) is None)


# ── Summary ─────────────────────────────────────────────────────────
print()
print(f"Total: {_passes} passed, {len(_failures)} failed")
if _failures:
    print("Failed checks:")
    for name in _failures:
        print(f"  - {name}")
    sys.exit(1)
sys.exit(0)
