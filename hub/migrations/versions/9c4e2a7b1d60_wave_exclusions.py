"""Cross-session wave exclusions

Revision ID: 9c4e2a7b1d60
Revises: b7e1c4f9a2d3
Create Date: 2026-11-22 16:18:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '9c4e2a7b1d60'
down_revision: Union[str, None] = 'b7e1c4f9a2d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'wave_exclusions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('ref', sa.String(length=64), nullable=False),
        sa.Column('fingerprint', sa.String(length=64), nullable=True),
        sa.Column('reason', sa.String(length=16), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'ref', 'reason', name='uq_wave_exclusions_user_ref_reason'),
    )
    op.create_index('ix_wave_exclusions_user_id', 'wave_exclusions', ['user_id'], unique=False)
    op.create_index('ix_wave_exclusions_user_expires', 'wave_exclusions', ['user_id', 'expires_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_wave_exclusions_user_expires', table_name='wave_exclusions')
    op.drop_index('ix_wave_exclusions_user_id', table_name='wave_exclusions')
    op.drop_table('wave_exclusions')
