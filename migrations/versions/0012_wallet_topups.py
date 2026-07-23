"""wallet_topups

Revision ID: 0012_wallet_topups
Revises: 0011_operator_payments
Create Date: 2026-07-23 12:00:00.000000

Купівля kWh-пакетів через Monobank-еквайринг ОПЕРАТОРА (замість тестового
Telegram Payments) — buy-side гаманця.

Чому окрема таблиця, а не 'type' колонка в operator_payments (обидва
варіанти розглядались на дизайн-рев'ю):

  reconcile_operators.py (Промпт 5) вважає БУДЬ-ЯКИЙ рядок operator_payments
  зі status='success' без прив'язаної operator_sessions розбіжністю, що
  потребує ручного розбору (list_success_payments_without_session). Wallet
  topup ніколи не матиме сесії станції — якби він жив у тій самій таблиці,
  кожен проданий пакет був би хибним алертом звірки, або довелось би
  переписувати логіку й тести Промпту 5. Окрема таблиця лишає
  operator_payments і всю звірку навколо неї недоторканими.

Чому user_id NOT NULL (на відміну від operator_payments, де його взагалі
немає): тут, на відміну від анонімного QR-флоу станції, водій — завжди
зареєстрований користувач бота (users.user_id).

invoice_id унікальний у межах ОПЕРАТОРА (той самий принцип, що в
operator_payments, 0011) — інвойси створюються токеном мерчанта конкретного
оператора, глобальний UNIQUE дав би одному оператору змогу «зайняти»
ідентифікатор іншого.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0012_wallet_topups'
down_revision: Union[str, Sequence[str], None] = '0011_operator_payments'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("""
    CREATE TABLE IF NOT EXISTS wallet_topups (
        id SERIAL PRIMARY KEY,
        operator_id INTEGER NOT NULL REFERENCES operators(id) ON DELETE CASCADE,
        user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
        invoice_id VARCHAR(100) NOT NULL,
        package VARCHAR(20) NOT NULL CHECK (package IN ('pack_50', 'pack_100')),
        kwh NUMERIC(10, 2) NOT NULL CHECK (kwh > 0),
        amount_uah NUMERIC(12, 2) NOT NULL CHECK (amount_uah >= 0),
        status VARCHAR(20) NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'success', 'failed', 'expired', 'reversed')),
        payload JSONB,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (operator_id, invoice_id)
    );
    """)

    op.execute("CREATE INDEX IF NOT EXISTS idx_wallet_topups_operator_created ON wallet_topups(operator_id, created_at DESC);")
    op.execute("CREATE INDEX IF NOT EXISTS idx_wallet_topups_user ON wallet_topups(user_id);")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TABLE IF EXISTS wallet_topups;")
