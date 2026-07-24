"""ocpp_transactions

Revision ID: 0014_ocpp_transactions
Revises: 0013_ocpp_station_fields
Create Date: 2026-07-24 10:00:00.000000

OCPP Промпт 3b — транзакції + метринг. Три нові поля на operator_sessions,
через ALTER TABLE (той самий патерн, що 0013 для operator_stations):
операційна таблиця вже існує (0010), новий CREATE TABLE не потрібен.

  * ocpp_transaction_id INTEGER — Central System сама призначає transactionId
    у відповіді на StartTransaction (за специфікацією 1.6 CP його НЕ знає
    заздалегідь) — тут це просто operator_sessions.id тієї ж сесії,
    записаний у власну колонку (не PK напряму), щоб MeterValues/
    StopTransaction могли знайти сесію через БД навіть після
    перепідключення станції, коли in-memory стан ChargePoint втрачено.
  * meter_start_wh / meter_stop_wh BIGINT — сирі покази лічильника (Вт·год,
    як їх передає OCPP-повідомлення) на старті й фініші. BIGINT, а не
    INTEGER: реальне залізо часто передає НАКОПИЧУВАЛЬНИЙ показ лічильника
    (весь час служби станції), не лише за сесію, — за роки може вийти за
    межі int4. kwh (наявна колонка) далі — похідне значення
    (meter_stop_wh - meter_start_wh) / 1000 у Decimal, для білінгу.

Ідемпотентність:
  * uq_operator_sessions_ocpp_transaction — transactionId унікальний У МЕЖАХ
    оператора (той самий принцип, що invoice_id в operator_payments/
    wallet_topups); попутно є lookup-індексом для get_session_by_ocpp_
    transaction_id() (operator_id, ocpp_transaction_id — ті самі провідні
    колонки, що й у WHERE).
  * uq_operator_sessions_one_active_ocpp_per_station — щонайбільше ОДНА
    'charging'-сесія на станцію серед сесій із призначеним
    ocpp_transaction_id. Умова `ocpp_transaction_id IS NOT NULL` НАВМИСНА:
    статус 'charging' уже використовує наявний ручний money-флоу
    (confirm_station_switched_on, app/handlers/operator_billing.py), де
    ocpp_transaction_id завжди NULL — цей індекс той флоу взагалі не бачить,
    нуль перетину з ним.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0014_ocpp_transactions'
down_revision: Union[str, Sequence[str], None] = '0013_ocpp_station_fields'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TABLE operator_sessions ADD COLUMN IF NOT EXISTS ocpp_transaction_id INTEGER;")
    op.execute("ALTER TABLE operator_sessions ADD COLUMN IF NOT EXISTS meter_start_wh BIGINT;")
    op.execute("ALTER TABLE operator_sessions ADD COLUMN IF NOT EXISTS meter_stop_wh BIGINT;")

    op.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS uq_operator_sessions_ocpp_transaction
        ON operator_sessions(operator_id, ocpp_transaction_id) WHERE ocpp_transaction_id IS NOT NULL;
    """)
    op.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS uq_operator_sessions_one_active_ocpp_per_station
        ON operator_sessions(station_id) WHERE status = 'charging' AND ocpp_transaction_id IS NOT NULL;
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS uq_operator_sessions_one_active_ocpp_per_station;")
    op.execute("DROP INDEX IF EXISTS uq_operator_sessions_ocpp_transaction;")
    op.execute("ALTER TABLE operator_sessions DROP COLUMN IF EXISTS meter_stop_wh;")
    op.execute("ALTER TABLE operator_sessions DROP COLUMN IF EXISTS meter_start_wh;")
    op.execute("ALTER TABLE operator_sessions DROP COLUMN IF EXISTS ocpp_transaction_id;")
