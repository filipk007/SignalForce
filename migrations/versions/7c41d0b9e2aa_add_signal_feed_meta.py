"""Add source schedule and cursor to signal feeds.

Revision ID: 7c41d0b9e2aa
Revises: 58fe9645d4ae
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "7c41d0b9e2aa"
down_revision: str = "58fe9645d4ae"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("signal_feeds", sa.Column("meta", JSONB()))


def downgrade() -> None:
    # Intake sources re-read their first window after the cursor is dropped.
    op.drop_column("signal_feeds", "meta")
