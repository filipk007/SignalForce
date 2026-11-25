"""Add tenant feeds and durable external signals.

Revision ID: 58fe9645d4ae
Revises: b7e1d94c5a20
"""

import sqlalchemy as sa
from alembic import op

revision: str = "58fe9645d4ae"
down_revision: str = "b7e1d94c5a20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("signal_events", sa.Column("external_id", sa.String(64)))
    op.add_column("signal_events", sa.Column("intent_until", sa.DateTime(timezone=True)))
    op.alter_column("signal_events", "account_id", existing_type=sa.Integer(), nullable=True)
    op.create_unique_constraint(
        "uq_signal_external", "signal_events", ["tenant_id", "signal_type", "external_id"]
    )
    op.create_check_constraint(
        "ck_signal_account_or_external",
        "signal_events",
        "account_id IS NOT NULL OR external_id IS NOT NULL",
    )
    op.add_column("scan_runs", sa.Column("feed_key", sa.String(64)))
    op.create_table(
        "signal_feeds",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("feed_key", sa.String(64), nullable=False),
        sa.Column("last_success", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("tenant_id", "source_type", "feed_key", name="uq_signal_feed_key"),
    )
    op.create_table(
        "bk2021_feed_members",
        sa.Column("feed_id", sa.Integer(), sa.ForeignKey("signal_feeds.id"), primary_key=True),
        sa.Column(
            "signal_event_id", sa.Integer(), sa.ForeignKey("signal_events.id"), primary_key=True
        ),
        sa.Column("min_capacity_kwh", sa.Integer(), nullable=False),
    )
    op.create_index("ix_signal_feed_member_event", "bk2021_feed_members", ["signal_event_id"])


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT 1 FROM signal_feeds LIMIT 1")).first():
        raise RuntimeError("BK2021 feed data exists. Restore a pre-migration backup to roll back.")
    op.drop_index("ix_signal_feed_member_event", table_name="bk2021_feed_members")
    op.drop_table("bk2021_feed_members")
    op.drop_table("signal_feeds")
    op.drop_column("scan_runs", "feed_key")
    op.drop_constraint("ck_signal_account_or_external", "signal_events", type_="check")
    op.drop_constraint("uq_signal_external", "signal_events", type_="unique")
    op.alter_column("signal_events", "account_id", existing_type=sa.Integer(), nullable=False)
    op.drop_column("signal_events", "intent_until")
    op.drop_column("signal_events", "external_id")
