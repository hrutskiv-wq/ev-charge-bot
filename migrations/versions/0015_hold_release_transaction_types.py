"""hold_release_transaction_types

Revision ID: 0015_hold_release_types
Revises: 0014_ocpp_transactions
Create Date: 2026-07-24 12:00:00.000000

OCPP Промпт 3c-i — резервація kWh-балансу (модель A: заблокувати ліміт
наперед, списати факт на StopTransaction, повернути залишок).

Додає 'hold'/'release' до enum transaction_type — той самий патерн, що
0008_add_refund_transaction_type.py для 'refund':
  * 'hold' — ДЕБЕТ: резерв ліміту на сесію зарядки, ще ДО того, як відома
    фактична енергія (ставиться в create_charging_reservation(), до
    RemoteStartTransaction).
  * 'release' — КРЕДИТ: звільнення НЕВИКОРИСТАНОЇ частини резерву на
    StopTransaction (ліміт − факт), або повне звільнення застряглого
    резерву звіркою (reconcile_charging_reservations.py). Окремий тип, а
    не 'deposit'/'refund' — щоб повернення надлишку холду не плуталось зі
    звичайним поповненням чи компенсацією у звітності.

ALTER TYPE ... ADD VALUE не можна використати в тій самій транзакції, де
значення одразу застосовується — ця міграція лише додає значення, нічого
більше, той самий безпечний, уже перевірений у проді патерн ('refund').
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0015_hold_release_types'
down_revision: Union[str, Sequence[str], None] = '0014_ocpp_transactions'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TYPE transaction_type ADD VALUE IF NOT EXISTS 'hold';")
    op.execute("ALTER TYPE transaction_type ADD VALUE IF NOT EXISTS 'release';")


def downgrade() -> None:
    """Downgrade schema."""
    # PostgreSQL не підтримує видалення значення з ENUM напряму (немає
    # ALTER TYPE ... DROP VALUE). Той самий задокументований компроміс, що
    # й у 0008_add_refund_transaction_type.py — відкат вручну, з бекапом.
    pass
