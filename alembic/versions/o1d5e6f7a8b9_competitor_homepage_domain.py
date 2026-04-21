"""Competitor.homepage_domain with one-time backfill.

Revision ID: o1d5e6f7a8b9
Revises: n0b4c5d6e7f8
Create Date: 2026-04-22 00:00:00.000000

Adds a nullable homepage_domain column used for logo lookup and anywhere we
need a canonical company domain. Backfills existing rows by deriving an apex
from the first careers/newsroom domain (stripping scheme, path, and common
non-canonical subdomain prefixes like careers./jobs./www.). Rows where no
usable domain exists stay NULL — the UI just renders no logo.
"""
import json

from alembic import op
import sqlalchemy as sa


revision = 'o1d5e6f7a8b9'
down_revision = 'n0b4c5d6e7f8'
branch_labels = None
depends_on = None


_LOGO_SUBDOMAIN_PREFIXES = (
    "www.", "careers.", "jobs.", "blog.", "newsroom.", "news.",
    "media.", "press.", "about.",
)


def _derive_apex(careers_raw, newsroom_raw) -> str | None:
    """Match app.ui._competitor_logo_domain exactly so backfilled values line
    up with what the old render-time derivation would have produced."""
    def _parse(raw):
        if isinstance(raw, str):
            try:
                return json.loads(raw) or []
            except Exception:
                return []
        return list(raw or [])

    for s in _parse(careers_raw) + _parse(newsroom_raw):
        if not s:
            continue
        s = str(s).strip()
        if "//" in s:
            s = s.split("//", 1)[1]
        host = s.split("/", 1)[0].strip().lower()
        for prefix in _LOGO_SUBDOMAIN_PREFIXES:
            if host.startswith(prefix):
                host = host[len(prefix):]
                break
        if host:
            return host
    return None


def upgrade() -> None:
    op.add_column(
        "competitors",
        sa.Column("homepage_domain", sa.String(length=128), nullable=True),
    )

    bind = op.get_bind()
    rows = bind.execute(sa.text(
        "SELECT id, careers_domains, newsroom_domains FROM competitors"
    )).fetchall()
    for row in rows:
        apex = _derive_apex(row.careers_domains, row.newsroom_domains)
        if apex:
            bind.execute(
                sa.text("UPDATE competitors SET homepage_domain = :d WHERE id = :i"),
                {"d": apex, "i": row.id},
            )


def downgrade() -> None:
    with op.batch_alter_table("competitors") as batch:
        batch.drop_column("homepage_domain")
