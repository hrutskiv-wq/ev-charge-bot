"""add_refund_transaction_type

Revision ID: 0008_add_refund_type
Revises: 0007_ocpi_locations_module
Create Date: 2026-07-17 15:00:00.000000

Додає значення 'refund' до enum transaction_type. Це виправляє розходження
між цією Alembic-міграцією (яка досі мала лише 'deposit', 'withdrawal',
'bonus', 'correction') і app/database/connection.py::create_tables(), де
inline idempotent-бутстрап УЖЕ багато часу створює enum одразу з 'refund'
— але лише для СВІЖИХ баз (той блок обгорнутий у
`IF NOT EXISTS (SELECT 1 FROM pg_type ...)` і не чіпає вже існуючий тип).
Тобто на будь-якій вже розгорнутій базі (як прод) 'refund' насправді не
було в enum, доки не накотили цю міграцію — сама по собі наявність
рядка в connection.py нічого не гарантувала.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0008_add_refund_type'
down_revision: Union[str, Sequence[str], None] = '0007_ocpi_locations_module'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # IF NOT EXISTS (підтримується з PostgreSQL 12) — ідемпотентно, безпечно
    # накотити повторно, якщо хтось уже додав значення вручну.
    op.execute("ALTER TYPE transaction_type ADD VALUE IF NOT EXISTS 'refund';")


def downgrade() -> None:
    """Downgrade schema."""
    # PostgreSQL не підтримує видалення значення з ENUM напряму (немає
    # ALTER TYPE ... DROP VALUE). Єдиний спосіб — перестворити тип і
    # перезаписати всі залежні стовпці, що надто ризиковано робити
    # автоматично в downgrade фінансової таблиці (kw_transactions). Якщо
    # відкат справді потрібен — виконувати вручну, з бекапом наперед.
    pass
