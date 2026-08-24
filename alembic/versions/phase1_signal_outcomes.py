"""Phase 1 F7: signal_outcomes table for outcome tracking.

Revision ID: phase1_f7_outcomes
Revises: 1a2b3c4d5e6f
"""
from alembic import op
import sqlalchemy as sa

revision = 'phase1_f7_outcomes'
down_revision = '1a2b3c4d5e6f'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'signal_outcomes',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('symbol', sa.String(50), nullable=False, index=True),
        sa.Column('timeframe', sa.String(10), nullable=False),
        sa.Column('market', sa.String(20), nullable=False),
        sa.Column('provider', sa.String(30), nullable=False),
        sa.Column('direction', sa.String(4), nullable=False),
        sa.Column('entry', sa.Float, nullable=False),
        sa.Column('stop_loss', sa.Float, nullable=False),
        sa.Column('tp1', sa.Float, nullable=False),
        sa.Column('tp2', sa.Float, nullable=False),
        sa.Column('tp3', sa.Float, nullable=False),
        sa.Column('risk_reward', sa.Float, nullable=False),
        sa.Column('quality', sa.Integer, nullable=False),
        sa.Column('confidence', sa.Integer, nullable=False),
        sa.Column('snapshot', sa.Text, nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='OPEN', index=True),
        sa.Column('opened_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('close_price', sa.Float, nullable=True),
        sa.Column('pnl_r', sa.Float, nullable=True),
    )


def downgrade() -> None:
    op.drop_table('signal_outcomes')
