"""operator_payments

Revision ID: 0011_operator_payments
Revises: 0010_white_label_tenants
Create Date: 2026-07-21 15:00:00.000000

Промпт 2a White-Label білінгу: платежі водіїв через еквайринг ОПЕРАТОРА.

Чому окрема таблиця, а не наявна payments:
  1. Водій НЕ реєструється — у нього немає users.user_id, а payments.user_id
     оголошено NOT NULL у b1b193e2bd7b (початкова схема). Тобто рядок для
     водійського платежу туди фізично не вставити.
  2. payments — журнал НАШИХ поповнень балансу (Monobank-банка + Telegram
     Invoice), його читає reconcile_payments.py. Домішування другої
     платіжної моделі зламало б звірку.
  3. Мультитенантність: кожна таблиця білінгу має operator_id.

Чому нова ревізія, а не правка 0010: 0010 уже змержена в main (PR #10) і
може бути накочена на прод будь-якої миті. Правка вже випущеної міграції —
рівно той механізм, що дав розходження схеми з 'refund' (PROJECT_CONTEXT,
п.8): у файлі одне, у реальній базі інше.

Заразом перенаправляємо operator_sessions.payment_id з payments(id) на
operator_payments(id): у 0010 він вказував на payments, що для водійських
сесій ніколи не спрацювало б з тієї самої причини (1).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0011_operator_payments'
down_revision: Union[str, Sequence[str], None] = '0010_white_label_tenants'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""

    # 1. Платежі водіїв. invoice_id унікальний У МЕЖАХ оператора, а не
    # глобально: інвойси створюються в різних мерчантах, і глобальний UNIQUE
    # дав би оператору А змогу «зайняти» ідентифікатор оператора Б.
    op.execute("""
    CREATE TABLE IF NOT EXISTS operator_payments (
        id SERIAL PRIMARY KEY,
        operator_id INTEGER NOT NULL REFERENCES operators(id) ON DELETE CASCADE,
        invoice_id VARCHAR(100) NOT NULL,
        amount_uah NUMERIC(12, 2) NOT NULL CHECK (amount_uah >= 0),
        status VARCHAR(20) NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'success', 'failed', 'expired', 'reversed')),
        payload JSONB,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (operator_id, invoice_id)
    );
    """)

    op.execute("CREATE INDEX IF NOT EXISTS idx_operator_payments_operator_created ON operator_payments(operator_id, created_at DESC);")

    # 2. operator_sessions.payment_id тепер вказує на operator_payments.
    # Ім'я обмеження — те, що Postgres згенерував у 0010 для inline
    # REFERENCES payments(id).
    op.execute("ALTER TABLE operator_sessions DROP CONSTRAINT IF EXISTS operator_sessions_payment_id_fkey;")
    op.execute("""
    ALTER TABLE operator_sessions
        ADD CONSTRAINT operator_sessions_payment_id_fkey
        FOREIGN KEY (payment_id) REFERENCES operator_payments(id) ON DELETE SET NULL;
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TABLE operator_sessions DROP CONSTRAINT IF EXISTS operator_sessions_payment_id_fkey;")
    op.execute("""
    ALTER TABLE operator_sessions
        ADD CONSTRAINT operator_sessions_payment_id_fkey
        FOREIGN KEY (payment_id) REFERENCES payments(id) ON DELETE SET NULL;
    """)
    op.execute("DROP TABLE IF EXISTS operator_payments;")
