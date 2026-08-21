"""kw_transactions_user_fk

Revision ID: 0020_kw_transactions_user_fk
Revises: 0019_add_telegram_provider
Create Date: 2026-08-21 16:00:00.000000

Зовнішній ключ `kw_transactions.user_id -> users(user_id)`.

**Навіщо.** У гілці Alembic не було ЖОДНОГО зовнішнього ключа на `users` —
ні з `payments`, ні з `kw_transactions`, ні з `ocpi_cdrs` (виміряно
`test_schema_parity_live.py` 21.08.2026). Прод стоїть на Alembic-гілці,
тому 15.07.2026 туди без жодної перешкоди ліг рядок журналу на +50 кВт·год
для `user_id`, якого немає в `users`. Записав його вебхук Monobank
(`app/api/payments.py`, видалений 28.07.2026) повз єдину точку входу.

Запобіжник у `update_user_balance()` (коміт `c14da54`) закриває лише той
шлях, що йде через саму функцію. **Ключ діє незалежно від шляху запису** —
тому клас закриває саме він, а не запобіжник. Розбір —
`docs/plan-orphan-balance-guard.md`.

`ON DELETE CASCADE` — дослівно як у бутстрапі (`create_tables()`, крок 5).
Сенс міграції в тому, щоб ЗАКРИТИ розрив паритету; інша семантика видалення
створила б новий замість закритого. Бутстрап тут не змінюється — рідкісний
випадок, коли міграція доганяє його, а не навпаки.

**Порядок жорсткий.** `ADD CONSTRAINT` без `NOT VALID` змушує Postgres
перевірити всі наявні рядки негайно: один осиротілий `user_id` — і міграція
падає з `ForeignKeyViolationError`. Тому спершу `scripts/repair_orphan_balance.py`
(коміт `a458b23`), і лише потім ця міграція. Передпольотна перевірка:

    SELECT DISTINCT t.user_id
    FROM kw_transactions t
    LEFT JOIN users u ON u.user_id = t.user_id
    WHERE u.user_id IS NULL;

Має повернути нуль рядків. `LEFT JOIN`, не `JOIN` — внутрішнє з'єднання не
бачить саме тих рядків, які шукаємо (`CLAUDE.md` §6b).

`NOT VALID` + окремий `VALIDATE CONSTRAINT` (щоб не тримати довгий лок)
тут не потрібні: журнал на проді — десятки рядків, не мільйони. Згадано,
щоб не виникало питання, чому не зроблено.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0020_kw_transactions_user_fk'
down_revision: Union[str, Sequence[str], None] = '0019_add_telegram_provider'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # IF NOT EXISTS для ADD CONSTRAINT у Postgres немає, тому ідемпотентність
    # робиться перевіркою в pg_constraint: на базі, піднятій бутстрапом,
    # ключ уже є (під іншим, згенерованим іменем не буде — бутстрап створює
    # його інлайном у CREATE TABLE, тож ім'я теж генерується; звідси перевірка
    # саме за парою «таблиця + колонка», а не за іменем).
    op.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON kcu.constraint_name = tc.constraint_name
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_schema = 'public'
              AND tc.table_name = 'kw_transactions'
              AND kcu.column_name = 'user_id'
        ) THEN
            ALTER TABLE kw_transactions
                ADD CONSTRAINT fk_kw_transactions_user
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE;
        END IF;
    END $$;
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TABLE kw_transactions DROP CONSTRAINT IF EXISTS fk_kw_transactions_user;")
