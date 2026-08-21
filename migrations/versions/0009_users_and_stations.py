"""users_and_stations

Revision ID: 0009_users_and_stations
Revises: 0008_add_refund_type
Create Date: 2026-08-20 12:00:00.000000

Дві базові таблиці, яких **не створювала жодна міграція**: `users` і `stations`.
Їх умів створити лише idempotent-бутстрап `create_tables()`
(`app/database/connection.py`), тобто робоча база виникала тільки як побічний
ефект старту застосунку.

Наслідок, заради якого міграція й пишеться: `alembic upgrade head` на порожній
базі **падав**. Дві міграції посилаються на `users` зовнішнім ключем —
`0012_wallet_topups` (`user_id ... REFERENCES users(user_id)`) і
`0016_charging_reservations` — і обидві не мали на що послатись. Тобто
твердження «Alembic — джерело правди схеми» (`CLAUDE.md` §3) було
неправдою: з міграцій не виходило бази взагалі.

**Чому слот 0009, а не нова голова.** Нова ревізія в кінці ланцюга проблеми
не вирішує: `alembic upgrade head` йде послідовно й помирає ще на 0012.
Таблиці мають з'явитись до неї. Слот 0009 був порожній (пропуск між 0008
і 0010 зафіксований у `docs/PROJECT_CONTEXT.md` §7) і стоїть саме там, де
треба. `0010_white_label_tenants.down_revision` переставлено на цю ревізію.

**DDL скопійовано з бутстрапу дослівно** — саме для того, щоб дві схеми
збіглися, а не розійшлися ще на один рядок. Дубль Alembic ↔ бутстрап
стріляв уже тричі (`refund` у 0008, відсутній FK `ocpi_cdrs.user_id`,
`payment_provider` без `telegram` у 0019), і мета цієї міграції — зменшити
розрив, а не додати новий.

**Про прод.** База в проді стоїть на 0019 і обидві таблиці має з бутстрапу.
Ця ревізія там ніколи не виконається — `upgrade head` уже no-op. Навіть
якби виконалась, `CREATE TABLE IF NOT EXISTS` нічого не змінює. Побічний
ефект вставки ревізії в середину ланцюга: `alembic history` показуватиме
0009 як пройдену, хоча вона не запускалась. Стан бази від цього не
залежить — таблиці на місці, DDL той самий.

**Downgrade навмисно порожній.** Зворотна дія до створення `users` — це
`DROP TABLE users`, тобто знищення всіх акаунтів і балансів разом із
`kw_transactions` по каскаду. Для базової таблиці з реальними грошима це
не «відкат міграції», а втрата даних; жоден сценарій відкату схеми такого
не потребує. Таблиці лишаються.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0009_users_and_stations'
down_revision: Union[str, Sequence[str], None] = '0008_add_refund_type'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Дослівна копія з create_tables() (app/database/connection.py, крок 2).
    op.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY,
        balance NUMERIC(10, 2) DEFAULT 0.00,
        discount NUMERIC(3, 2) DEFAULT 1.00,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # Дослівна копія з create_tables() (крок 3). Довідник станцій для
    # GET /api/stations; до операторських станцій білінгу (operator_stations,
    # 0010) стосунку не має — це різні сутності з різними ключами.
    op.execute("""
    CREATE TABLE IF NOT EXISTS stations (
        id VARCHAR(50) PRIMARY KEY,
        name VARCHAR(255),
        address TEXT,
        connectors TEXT,
        lat NUMERIC(9,6),
        lon NUMERIC(9,6),
        operator VARCHAR(255),
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)


def downgrade() -> None:
    """Downgrade schema."""
    # Порожньо навмисно — пояснення в докстрінгу модуля.
    pass
