"""charging_reservations

Revision ID: 0016_charging_reservations
Revises: 0015_hold_release_types
Create Date: 2026-07-24 12:30:00.000000

OCPP Промпт 3c-i — інфраструктура резервації + модель A (kWh-баланс).

Нова таблиця (не розширення operator_payments — той самий аргумент, що
для wallet_topups, міграція 0012: reconcile_operators.py трактує будь-який
'success'-платіж без сесії як розбіжність, а логіка резервацій зовсім інша
— тут узагалі нема "оплати" в сенсі operator_payments, лише блокування/
звільнення власного kWh-балансу водія).

  * id_tag VARCHAR(20) — те, що передається в remote_start_transaction()
    (app/api/ocpp_ws.py) і повертається станцією в Authorize/
    StartTransaction; за ним on_start_transaction знаходить резервацію.
    20 — точний ліміт довжини поля idTag за специфікацією OCPP 1.6J
    (JSON-схема бібліотеки ocpp: maxLength=20); генерується 16-символьним
    secrets.token_urlsafe(12) — з запасом.
  * payment_method CHECK IN ('kwh') — навмисно ЛИШЕ одне значення. Модель B
    (UAH, Monobank hold/finalize/cancel) — окремий промпт 3c-ii, після
    живого смоук-тесту hold-API; тоді CHECK розшириться, а не
    переписуватиметься.
  * status: pending (резерв поставлено, RemoteStart ще не підтверджено) ->
    active (StartTransaction прийшов, привʼязано operator_session_id) ->
    finalized (StopTransaction, залишок звільнено) АБО cancelled/expired
    (звірка добрала застряглу резервацію — reconcile_charging_reservations.py).
  * operator_session_id — nullable, заповнюється лише на активації
    (on_start_transaction); ON DELETE SET NULL, а не CASCADE — видалення
    сесії (якщо колись з'явиться) не має знищувати слід про резервацію.

Композитний FK (station_id, operator_id) — той самий патерн ізоляції
тенантів, що operator_sessions (0010): крос-тенантну резервацію
неможливо записати навіть навмисно.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0016_charging_reservations'
down_revision: Union[str, Sequence[str], None] = '0015_hold_release_types'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("""
    CREATE TABLE IF NOT EXISTS charging_reservations (
        id SERIAL PRIMARY KEY,
        operator_id INTEGER NOT NULL REFERENCES operators(id) ON DELETE CASCADE,
        station_id INTEGER NOT NULL,
        user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
        payment_method VARCHAR(10) NOT NULL CHECK (payment_method IN ('kwh')),
        reserved_kwh NUMERIC(10, 3) NOT NULL CHECK (reserved_kwh > 0),
        id_tag VARCHAR(20) NOT NULL UNIQUE,
        status VARCHAR(20) NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'active', 'finalized', 'cancelled', 'expired')),
        operator_session_id INTEGER REFERENCES operator_sessions(id) ON DELETE SET NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (station_id, operator_id)
            REFERENCES operator_stations (id, operator_id) ON DELETE CASCADE
    );
    """)

    op.execute("CREATE INDEX IF NOT EXISTS idx_charging_reservations_operator_created ON charging_reservations(operator_id, created_at DESC);")
    op.execute("CREATE INDEX IF NOT EXISTS idx_charging_reservations_session ON charging_reservations(operator_session_id);")
    op.execute("CREATE INDEX IF NOT EXISTS idx_charging_reservations_status_created ON charging_reservations(status, created_at);")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TABLE IF EXISTS charging_reservations;")
