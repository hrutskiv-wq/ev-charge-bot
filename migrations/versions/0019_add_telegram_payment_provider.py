"""add_telegram_payment_provider

Revision ID: 0019_add_telegram_provider
Revises: 0018_charging_reservations_uah
Create Date: 2026-08-06 09:00:00.000000

Додає значення 'telegram' до enum payment_provider.

Той самий клас розходження, що вже закривала міграція 0008 для 'refund',
і виявлений він тим самим способом — запитом до pg_enum на проді:

    SELECT enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid
    WHERE t.typname = 'payment_provider' ORDER BY enumsortorder;
    -- liqpay, monobank        (06.08.2026, прод)

Розходження між двома джерелами схеми:
  * b1b193e2bd7b_initial_schema.py  -> ENUM ('liqpay', 'monobank')
  * app/database/connection.py:79   -> ENUM ('monobank', 'telegram')

Обидва створюють тип лише «якщо не існує», тому на вже розгорнутій базі
(прод) діє варіант із першої міграції, і значення 'telegram' там НЕМАЄ.

Чому це важливо: app/handlers/user.py:1083 у обробнику successful_payment
робить

    INSERT INTO payments (..., provider, ...) VALUES (..., 'telegram', ...)

Тобто при першій же реальній оплаті через Telegram Payments запит впаде з
`invalid input value for enum payment_provider: "telegram"` — ПІСЛЯ того,
як гроші з користувача вже списані на боці Telegram. Баланс не нарахується,
рядка в payments не буде, і reconcile_payments.py такої втрати не побачить,
бо звіряє payments проти kw_transactions, а тут не з'явиться ні того, ні
іншого.

Зараз баг латентний лише тому, що TELEGRAM_PAYMENTS_ENABLED за замовчуванням
вимкнений. Вмикати цей флоу до накатування цієї міграції не можна.

'liqpay' зі старої міграції лишається в enum: коду LiqPay у репозиторії вже
немає, але видалення значення з ENUM у PostgreSQL неможливе без перестворення
типу, а робити це на фінансовій таблиці заради косметики не варто.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0019_add_telegram_provider'
down_revision: Union[str, Sequence[str], None] = '0018_charging_reservations_uah'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # IF NOT EXISTS (PostgreSQL 12+) — ідемпотентно: безпечно накотити
    # повторно, якщо значення вже додали вручну через psql.
    op.execute("ALTER TYPE payment_provider ADD VALUE IF NOT EXISTS 'telegram';")


def downgrade() -> None:
    """Downgrade schema."""
    # PostgreSQL не має ALTER TYPE ... DROP VALUE. Єдиний спосіб — перестворити
    # тип і перезаписати всі залежні стовпці, що надто ризиковано робити
    # автоматично на payments. Якщо відкат справді потрібен — вручну, з бекапом.
    pass
