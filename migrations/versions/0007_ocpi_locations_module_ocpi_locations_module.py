"""ocpi_locations_module

Revision ID: 0007_ocpi_locations_module
Revises: b1b193e2bd7b
Create Date: 2026-07-16 14:17:02.505766

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0007_ocpi_locations_module'
down_revision: Union[str, Sequence[str], None] = 'b1b193e2bd7b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
