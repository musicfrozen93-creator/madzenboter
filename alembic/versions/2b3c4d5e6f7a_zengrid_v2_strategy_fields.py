"""ZenGrid V2 strategy fields

Adds the V2 columns to existing deployments. All columns are nullable or
carry server defaults, so existing rows (live customer baskets, historical
signals, the current watchlist) remain fully valid:

  baskets:   template ('core'), risk_budget (0 = legacy basket, V1 stop
             behaviour preserved), wind_down (false), wind_down_at
  signals:   symbol_state, btc_state, relative_strength, alignment_score
  watchlist: tier ('core')

Fresh deployments get these columns from Base.metadata.create_all(); this
migration upgrades databases that predate V2.

Revision ID: 2b3c4d5e6f7a
Revises: 1a2b3c4d5e6f
Create Date: 2026-06-12 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2b3c4d5e6f7a'
down_revision: Union[str, None] = '1a2b3c4d5e6f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    """True if the column already exists (idempotent against create_all)."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return column in {c['name'] for c in inspector.get_columns(table)}


def upgrade() -> None:
    # ── baskets ──
    if not _has_column('baskets', 'template'):
        op.add_column(
            'baskets',
            sa.Column('template', sa.String(10), nullable=False,
                      server_default='core'),
        )
    if not _has_column('baskets', 'risk_budget'):
        op.add_column(
            'baskets',
            sa.Column('risk_budget', sa.Float(), nullable=False,
                      server_default='0'),
        )
    if not _has_column('baskets', 'wind_down'):
        op.add_column(
            'baskets',
            sa.Column('wind_down', sa.Boolean(), nullable=False,
                      server_default='false'),
        )
    if not _has_column('baskets', 'wind_down_at'):
        op.add_column(
            'baskets', sa.Column('wind_down_at', sa.Float(), nullable=True),
        )
    # Exit-state fields MUST be persisted — baskets are re-hydrated from the
    # database every management loop (audit findings C1/C2).
    if not _has_column('baskets', 'peak_roi'):
        op.add_column(
            'baskets',
            sa.Column('peak_roi', sa.Float(), nullable=False,
                      server_default='0'),
        )
    if not _has_column('baskets', 'be_armed'):
        op.add_column(
            'baskets',
            sa.Column('be_armed', sa.Boolean(), nullable=False,
                      server_default='false'),
        )

    # ── signals ──
    if not _has_column('signals', 'symbol_state'):
        op.add_column(
            'signals', sa.Column('symbol_state', sa.String(12), nullable=True),
        )
    if not _has_column('signals', 'btc_state'):
        op.add_column(
            'signals', sa.Column('btc_state', sa.String(12), nullable=True),
        )
    if not _has_column('signals', 'relative_strength'):
        op.add_column(
            'signals', sa.Column('relative_strength', sa.Float(), nullable=True),
        )
    if not _has_column('signals', 'alignment_score'):
        op.add_column(
            'signals', sa.Column('alignment_score', sa.Float(), nullable=True),
        )

    # ── watchlist ──
    if not _has_column('watchlist', 'tier'):
        op.add_column(
            'watchlist',
            sa.Column('tier', sa.String(12), nullable=True,
                      server_default='core'),
        )


def downgrade() -> None:
    op.drop_column('watchlist', 'tier')
    op.drop_column('signals', 'alignment_score')
    op.drop_column('signals', 'relative_strength')
    op.drop_column('signals', 'btc_state')
    op.drop_column('signals', 'symbol_state')
    op.drop_column('baskets', 'be_armed')
    op.drop_column('baskets', 'peak_roi')
    op.drop_column('baskets', 'wind_down_at')
    op.drop_column('baskets', 'wind_down')
    op.drop_column('baskets', 'risk_budget')
    op.drop_column('baskets', 'template')
