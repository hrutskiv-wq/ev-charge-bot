"""white_label_tenants

Revision ID: 0010_white_label_tenants
Revises: 0008_add_refund_type
Create Date: 2026-07-21 10:00:00.000000

Промпт 1 White-Label білінгу: тенанти (оператори зарядних станцій) та їхні
станції, сесії й журнал виплат.

Нумерація: файл названо 0010 відповідно до стратегічного документа
docs/evolt-white-label-bilinh-ta-p2p.md, але в ланцюгу Alembic ця ревізія
йде одразу після 0008_add_refund_type — міграцій 0009 у репозиторії немає
(перевірено по origin/main). Номер файлу і revision-ідентифікатор навмисно
не «підганялись» під ланцюг: важливий саме down_revision.

ДЗЕРКАЛО: ця схема продубльована idempotent-блоком у
app/database/operators_repo.py::init_operator_tables(), щоб застосунок
піднімався на чистій базі без ручного `alembic upgrade head` (та сама
конвенція, що й для create_tables()/init_ocpi_tables()). При зміні схеми
оновлювати ОБИДВА місця — за цим стежить test_operator_isolation.py::
test_migration_and_idempotent_bootstrap_declare_same_columns.

Статуси свідомо зроблено VARCHAR + CHECK, а не нативними ENUM: додавання
нового статусу — це ALTER одного CHECK-обмеження всередині транзакції, тоді
як ALTER TYPE ... ADD VALUE не відкочується і вже одного разу спричинив
розходження схеми між Alembic і бутстрапом (див. 0008_add_refund_transaction_type.py).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0010_white_label_tenants'
down_revision: Union[str, Sequence[str], None] = '0008_add_refund_type'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""

    # 1. Оператори (тенанти). monobank_token_encrypted зберігається лише в
    # зашифрованому вигляді (Fernet на ENCRYPTION_KEY, шифрування/дешифрування —
    # поза цим шаром) і НІКОЛИ не потрапляє в загальні SELECT'и репозиторію.
    op.execute("""
    CREATE TABLE IF NOT EXISTS operators (
        id SERIAL PRIMARY KEY,
        name VARCHAR(255) NOT NULL,
        phone VARCHAR(32),
        telegram_id BIGINT NOT NULL UNIQUE,
        status VARCHAR(20) NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'active', 'suspended')),
        commission_pct NUMERIC(5, 2) NOT NULL DEFAULT 4
            CHECK (commission_pct >= 0 AND commission_pct <= 100),
        monobank_token_encrypted TEXT,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 2. Станції оператора. qr_slug — публічний ключ сторінки оплати
    # /s/{qr_slug}, тому унікальний глобально і сам по собі є секретом.
    # UNIQUE (id, operator_id) існує заради композитного зовнішнього ключа
    # з operator_sessions — див. нижче.
    op.execute("""
    CREATE TABLE IF NOT EXISTS operator_stations (
        id SERIAL PRIMARY KEY,
        operator_id INTEGER NOT NULL REFERENCES operators(id) ON DELETE CASCADE,
        name VARCHAR(255) NOT NULL,
        address TEXT,
        lat NUMERIC(9, 6),
        lng NUMERIC(9, 6),
        connector_type VARCHAR(50),
        power_kw NUMERIC(6, 2),
        mode VARCHAR(10) NOT NULL DEFAULT 'manual'
            CHECK (mode IN ('manual', 'ocpp')),
        ocpp_charge_point_id VARCHAR(100) UNIQUE,
        tariff_uah_kwh NUMERIC(10, 2) NOT NULL CHECK (tariff_uah_kwh >= 0),
        tariff_uah_start NUMERIC(10, 2) CHECK (tariff_uah_start >= 0),
        qr_slug VARCHAR(32) NOT NULL UNIQUE,
        status VARCHAR(20) NOT NULL DEFAULT 'active'
            CHECK (status IN ('active', 'offline', 'disabled')),
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (id, operator_id)
    );
    """)

    # 3. Сесії зарядки. operator_id продубльовано зі станції НАВМИСНО:
    # (а) кожен запит кабінету фільтрується по operator_id без JOIN,
    # (б) композитний FK (station_id, operator_id) робить сесію, привʼязану
    #     до чужої станції, неможливою на рівні БД, а не лише в коді.
    # Водій не реєструється — driver_contact потрібен лише щоб надіслати чек.
    op.execute("""
    CREATE TABLE IF NOT EXISTS operator_sessions (
        id SERIAL PRIMARY KEY,
        operator_id INTEGER NOT NULL,
        station_id INTEGER NOT NULL,
        started_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
        ended_at TIMESTAMP WITH TIME ZONE,
        kwh NUMERIC(10, 3) CHECK (kwh >= 0),
        amount_uah NUMERIC(12, 2) CHECK (amount_uah >= 0),
        payment_id INTEGER REFERENCES payments(id) ON DELETE SET NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'paid', 'charging', 'completed', 'failed', 'refunded')),
        driver_contact VARCHAR(64),
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (station_id, operator_id)
            REFERENCES operator_stations (id, operator_id) ON DELETE CASCADE
    );
    """)

    # 4. Журнал розрахунків з оператором — незмінний, зі ЗНАКОВОЮ сумою
    # (дохід +, комісія/підписка/виплата −), за тією ж філософією, що й
    # kw_transactions. Кешованої колонки «баланс оператора» свідомо НЕМАЄ:
    # баланс = SUM(amount_uah). Це прибирає цілий клас багів «кеш розійшовся
    # з журналом», який у цьому проєкті вже виправлявся тричі.
    op.execute("""
    CREATE TABLE IF NOT EXISTS operator_payout_ledger (
        id SERIAL PRIMARY KEY,
        operator_id INTEGER NOT NULL REFERENCES operators(id) ON DELETE CASCADE,
        session_id INTEGER REFERENCES operator_sessions(id) ON DELETE SET NULL,
        type VARCHAR(24) NOT NULL
            CHECK (type IN ('session_income', 'platform_commission',
                            'subscription_fee', 'payout', 'adjustment')),
        amount_uah NUMERIC(12, 2) NOT NULL,
        description TEXT,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 5. Індекси. Кожен «гарячий» запит кабінету починається з operator_id,
    # тому він стоїть першим у складених індексах.
    op.execute("CREATE INDEX IF NOT EXISTS idx_operator_stations_operator ON operator_stations(operator_id);")
    op.execute("CREATE INDEX IF NOT EXISTS idx_operator_sessions_operator_started ON operator_sessions(operator_id, started_at DESC);")
    op.execute("CREATE INDEX IF NOT EXISTS idx_operator_sessions_station ON operator_sessions(station_id);")
    op.execute("CREATE INDEX IF NOT EXISTS idx_operator_sessions_payment ON operator_sessions(payment_id);")
    op.execute("CREATE INDEX IF NOT EXISTS idx_operator_ledger_operator_created ON operator_payout_ledger(operator_id, created_at DESC);")

    # 6. Ідемпотентність розрахунків — на рівні БД, а не тільки в коді.
    #
    # uq_ledger_session_income: одна сесія дає рівно один рядок доходу і
    # один рядок комісії. Без цього повторний webhook Monobank, ретрай або
    # подвійний клік оператора нарахували б дохід двічі — а журнал
    # незмінний, тобто помилку довелось би виправляти рядком 'adjustment'
    # вручну. Індекс частковий: рядки без session_id ('payout',
    # 'subscription_fee', 'adjustment') під обмеження не підпадають і
    # можуть повторюватись скільки завгодно.
    #
    # uq_sessions_payment: один інвойс Monobank не може бути привʼязаний до
    # двох сесій. Це та сама гарантія, що invoice_id UNIQUE у payments, але
    # для білінгу операторів.
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_ledger_session_income ON operator_payout_ledger(session_id, type) WHERE session_id IS NOT NULL AND type IN ('session_income', 'platform_commission');")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_sessions_payment ON operator_sessions(payment_id) WHERE payment_id IS NOT NULL;")


def downgrade() -> None:
    """Downgrade schema."""
    # Порядок зворотний до створення через зовнішні ключі.
    op.execute("DROP TABLE IF EXISTS operator_payout_ledger;")
    op.execute("DROP TABLE IF EXISTS operator_sessions;")
    op.execute("DROP TABLE IF EXISTS operator_stations;")
    op.execute("DROP TABLE IF EXISTS operators;")
