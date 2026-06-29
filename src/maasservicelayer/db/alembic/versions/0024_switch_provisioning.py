# Copyright 2026 Canonical Ltd. This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""switch_provisioning

Revision ID: 0024
Revises: 0023
Create Date: 2026-07-01 00:00:00.000000+00:00

"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "maasserver_switch",
        sa.Column(
            "switch_uuid",
            sa.UUID(),
            nullable=True,
        ),
    )
    op.add_column(
        "maasserver_switch",
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default="NOT_PROVISIONED",
        ),
    )

    op.execute(
        "UPDATE maasserver_switch SET switch_uuid = gen_random_uuid() WHERE switch_uuid IS NULL"
    )

    op.alter_column("maasserver_switch", "switch_uuid", nullable=False)

    op.create_unique_constraint(
        "maasserver_switch_switch_uuid_key",
        "maasserver_switch",
        ["switch_uuid"],
    )

    op.create_table(
        "switch_scripts",
        sa.Column(
            "id", sa.BigInteger(), sa.Identity(always=False), nullable=False
        ),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated", sa.DateTime(timezone=True), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("content", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="switch_scripts_name_key"),
    )

    op.create_table(
        "switch_script_assignment",
        sa.Column(
            "id", sa.BigInteger(), sa.Identity(always=False), nullable=False
        ),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated", sa.DateTime(timezone=True), nullable=False),
        sa.Column("switch_id", sa.BigInteger(), nullable=False),
        sa.Column("script_id", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["switch_id"],
            ["maasserver_switch.id"],
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["script_id"],
            ["switch_scripts.id"],
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.UniqueConstraint(
            "switch_id",
            "script_id",
            name="switch_script_assignment_switch_script_uniq",
        ),
    )

    op.create_table(
        "switch_logs",
        sa.Column(
            "id", sa.BigInteger(), sa.Identity(always=False), nullable=False
        ),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated", sa.DateTime(timezone=True), nullable=False),
        sa.Column("switch_id", sa.BigInteger(), nullable=False),
        sa.Column("log_category", sa.String(32), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=False),
        sa.Column("output", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["switch_id"],
            ["maasserver_switch.id"],
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.CheckConstraint(
            "log_category IN ('WRAPPER', 'NOS_INSTALLATION', 'PROVISIONING_SCRIPT')",
            name="switch_logs_log_category_check",
        ),
    )

    op.create_index(
        "switch_logs_switch_id_idx",
        "switch_logs",
        ["switch_id"],
    )
    op.create_index(
        "switch_script_assignment_switch_id_idx",
        "switch_script_assignment",
        ["switch_id"],
    )


def downgrade() -> None:
    # We do not support migration downgrade
    pass
