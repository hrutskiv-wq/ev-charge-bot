"""initial_schema

Revision ID: b1b193e2bd7b
Revises: None
Create Date: 2026-07-15 12:21:58.842088

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b1b193e2bd7b'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Безпечно створюємо ENUM-типи (якщо вони вже існують — помилки не буде)
    op.execute("""
    DO $$ BEGIN
        CREATE TYPE payment_provider AS ENUM ('liqpay', 'monobank');
    EXCEPTION
        WHEN duplicate_object THEN null;
    END $$;
    """)
    op.execute("""
    DO $$ BEGIN
        CREATE TYPE payment_status AS ENUM ('pending', 'success', 'failed', 'refunded');
    EXCEPTION
        WHEN duplicate_object THEN null;
    END $$;
    """)
    op.execute("""
    DO $$ BEGIN
        CREATE TYPE transaction_type AS ENUM ('deposit', 'withdrawal', 'bonus', 'correction');
    EXCEPTION
        WHEN duplicate_object THEN null;
    END $$;
    """)

    # 2. Створюємо таблицю payments (якщо не існує)
    op.execute("""
    CREATE TABLE IF NOT EXISTS payments (
        id SERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        invoice_id VARCHAR(100) UNIQUE NOT NULL,
        amount NUMERIC(10, 2) NOT NULL,
        provider payment_provider NOT NULL,
        status payment_status NOT NULL DEFAULT 'pending',
        payload JSONB,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 3. Створюємо таблицю kw_transactions (якщо не існує)
    op.execute("""
    CREATE TABLE IF NOT EXISTS kw_transactions (
        id SERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        type transaction_type NOT NULL,
        amount NUMERIC(8, 2) NOT NULL,
        payment_id INTEGER REFERENCES payments(id) ON DELETE SET NULL,
        session_id VARCHAR(100),
        description TEXT,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 4. Створюємо таблицю ocpi_cdrs (якщо не існує)
    op.execute("""
    CREATE TABLE IF NOT EXISTS ocpi_cdrs (
        id SERIAL PRIMARY KEY,
        cdr_id VARCHAR(100) UNIQUE NOT NULL,
        user_id BIGINT NOT NULL,
        session_id VARCHAR(100) NOT NULL,
        total_energy NUMERIC(8, 2) NOT NULL,
        total_cost NUMERIC(10, 2) NOT NULL,
        raw_payload JSONB NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 5. Безпечно створюємо індекси
    op.execute("CREATE INDEX IF NOT EXISTS idx_payments_user_id ON payments(user_id);")
    op.execute("CREATE INDEX IF NOT EXISTS idx_kw_transactions_user_id ON kw_transactions(user_id);")
    op.execute("CREATE INDEX IF NOT EXISTS idx_ocpi_cdrs_user_id ON ocpi_cdrs(user_id);")
    op.execute("CREATE INDEX IF NOT EXISTS idx_ocpi_cdrs_session_id ON ocpi_cdrs(session_id);")


def downgrade() -> None:
    # Видалення таблиць
    op.execute("DROP TABLE IF EXISTS ocpi_cdrs;")
    op.execute("DROP TABLE IF EXISTS kw_transactions;")
    op.execute("DROP TABLE IF EXISTS payments;")

    # Видалення ENUM-типів
    op.execute("DROP TYPE IF EXISTS transaction_type;")
    op.execute("DROP TYPE IF EXISTS payment_status;")
    op.execute("DROP TYPE IF EXISTS payment_provider;")
