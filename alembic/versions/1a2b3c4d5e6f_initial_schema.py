"""Initial schema

Revision ID: 1a2b3c4d5e6f
Revises: None
Create Date: 2026-06-04 19:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1a2b3c4d5e6f'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # All tables are automatically created via Base.metadata.create_all()
    # in the Database.initialize() call on application startup.
    # This migration acts as the baseline version.
    pass


def downgrade() -> None:
    pass
