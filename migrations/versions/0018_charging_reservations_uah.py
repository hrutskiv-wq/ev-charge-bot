"""charging_reservations_uah

Revision ID: 0018_charging_reservations_uah
Revises: 0017_operator_self_service
Create Date: 2026-07-29 12:00:00.000000

OCPP Промпт 3c-ii — модель B (грн через Monobank hold/finalize/cancel).
Розширює `charging_reservations` (0016, модель A на kWh), а не заводить
нову таблицю: статуси pending/active/finalized/cancelled/expired і сам
механізм «резерв -> факт -> звільнення» — одна концепція незалежно від
валюти, і reconcile_charging_reservations.py має лишитись єдиним для обох
методів оплати. Гейт цієї моделі — живий смоук-тест 28-29.07.2026 (див.
docs/SESSION_STATE.md) — пройдено ДО цього коду.

  * `payment_method` CHECK: 'kwh' -> 'kwh'/'uah' (докстрінг 0016 прямо це
    анонсував: "модель B окремим промптом розширить, не перепише").
  * `status` CHECK: додано ДВА нових значення:
      - 'awaiting_hold' — інвойс створено в банку, гроші ще НЕ заблоковані
        (RemoteStart ще не надсилали). У моделі A такого стану немає — там
        hold миттєвий, у тій самій транзакції, що й створення резерву;
        тут hold підтверджується АСИНХРОННО, вебхуком банку.
      - 'settling' — атомарна ЛОКАЛЬНА заявка «іду розраховуватись із
        банком за цю резервацію», яка ставиться ОДНИМ мʼютексним UPDATE
        ПЕРЕД самим викликом finalize/cancel. Це головний захист від
        подвійного звернення до банку — ми НЕ покладаємось на те, що банк
        сам захищає від повторного finalize (це припущення нашого ж мока,
        не підтверджений живим смоуком факт).
  * `reserved_kwh`/`user_id` — NOT NULL знято: uah-рядок не резервує kWh-
    баланс і не обов'язково належить зареєстрованому водієві (модель B —
    оплата карткою через Monobank, водій може бути взагалі не в боті).
  * Нові nullable-колонки: `reserved_uah` (утримана сума), `invoice_id`
    (інвойс банку — ключ для вебхука/звірки), `final_amount_uah`
    (фактично списане після finalize/cancel), `driver_contact` (той самий
    патерн, що `operator_sessions.driver_contact` у QR-флоу).
  * Дискримінована CHECK-умова — рівно ОДНА з двох форм рядка можлива,
    ніколи не "напівпорожня": той самий стиль, що композитні FK і часткові
    унікальні індекси в проєкті.
  * Частковий унікальний індекс на `invoice_id` — один інвойс банку не
    може належати двом резерваціям (той самий аргумент, що
    `uq_sessions_payment`).

ВАЖЛИВО (fail-closed для наявних, модель-A функцій): після цієї міграції
`release_reservation_hold()`, `list_stale_pending_reservations()`,
`list_stale_active_reservations()` (app/database/operators_repo.py)
фільтрують `payment_method = 'kwh'` у WHERE — без цього uah-рядок з
user_id=NULL дійшов би до update_user_balance(user_id=NULL) і впав би
NotNullViolation. Це зміна в Python-коді репозиторію, не в цій міграції,
але задокументована тут, бо є прямим наслідком дозволу NULL user_id.

CHECK-обмеження (enum-розширення, дискримінована умова) наявний generic
schema-drift тест (test_operator_isolation.py) НЕ парсить — лише ADD
COLUMN. Тому цей клас розбіжності («є в міграції, не в бутстрапі, чи
навпаки» — урок 'refund', PROJECT_CONTEXT.md) закритий окремим явним
тестом на текст CHECK-виразів, а не generic-регексом.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0018_charging_reservations_uah'
down_revision: Union[str, Sequence[str], None] = '0017_operator_self_service'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # payment_method: 'kwh' -> 'kwh'/'uah'. Ім'я обмеження — стандартне
    # автопризначене Postgres для одноколонкового inline CHECK ("<table>_
    # <column>_check"), яке отримав рядок CHECK (payment_method IN ('kwh'))
    # у CREATE TABLE міграції 0016.
    op.execute("ALTER TABLE charging_reservations DROP CONSTRAINT IF EXISTS charging_reservations_payment_method_check;")
    op.execute("""
    ALTER TABLE charging_reservations
        ADD CONSTRAINT charging_reservations_payment_method_check
        CHECK (payment_method IN ('kwh', 'uah'));
    """)

    # status: +'awaiting_hold', +'settling'.
    op.execute("ALTER TABLE charging_reservations DROP CONSTRAINT IF EXISTS charging_reservations_status_check;")
    op.execute("""
    ALTER TABLE charging_reservations
        ADD CONSTRAINT charging_reservations_status_check
        CHECK (status IN ('awaiting_hold', 'pending', 'active', 'settling', 'finalized', 'cancelled', 'expired'));
    """)

    # uah-рядок не резервує kWh-баланс і не обов'язково має зареєстрованого
    # водія — обидва поля стають опційними. reserved_kwh > 0 CHECK (окреме
    # обмеження, не чіпається тут) NULL-значення пропускає тривіально.
    op.execute("ALTER TABLE charging_reservations ALTER COLUMN reserved_kwh DROP NOT NULL;")
    op.execute("ALTER TABLE charging_reservations ALTER COLUMN user_id DROP NOT NULL;")

    op.execute("ALTER TABLE charging_reservations ADD COLUMN IF NOT EXISTS reserved_uah NUMERIC(10, 2);")
    op.execute("ALTER TABLE charging_reservations ADD COLUMN IF NOT EXISTS invoice_id VARCHAR(64);")
    op.execute("ALTER TABLE charging_reservations ADD COLUMN IF NOT EXISTS final_amount_uah NUMERIC(10, 2);")
    op.execute("ALTER TABLE charging_reservations ADD COLUMN IF NOT EXISTS driver_contact VARCHAR(64);")

    # Дискримінована умова: рівно kwh-форма АБО рівно uah-форма, ніколи
    # напівпорожньо. Наявні (kwh) рядки задовольняють її автоматично —
    # reserved_uah/invoice_id щойно додані й для них NULL.
    op.execute("""
    ALTER TABLE charging_reservations
        ADD CONSTRAINT charging_reservations_payment_shape_check
        CHECK (
            (payment_method = 'kwh' AND reserved_kwh IS NOT NULL AND user_id IS NOT NULL
             AND reserved_uah IS NULL AND invoice_id IS NULL)
            OR
            (payment_method = 'uah' AND reserved_uah IS NOT NULL AND invoice_id IS NOT NULL
             AND reserved_kwh IS NULL)
        );
    """)

    op.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS uq_charging_reservations_invoice_id
        ON charging_reservations(invoice_id) WHERE invoice_id IS NOT NULL;
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS uq_charging_reservations_invoice_id;")
    op.execute("ALTER TABLE charging_reservations DROP CONSTRAINT IF EXISTS charging_reservations_payment_shape_check;")
    op.execute("ALTER TABLE charging_reservations DROP COLUMN IF EXISTS driver_contact;")
    op.execute("ALTER TABLE charging_reservations DROP COLUMN IF EXISTS final_amount_uah;")
    op.execute("ALTER TABLE charging_reservations DROP COLUMN IF EXISTS invoice_id;")
    op.execute("ALTER TABLE charging_reservations DROP COLUMN IF EXISTS reserved_uah;")
    # NOT NULL назад — БЕЗПЕЧНО лише на чистій базі без uah-рядків
    # (той самий задокументований компроміс, що ALTER TYPE DROP VALUE у
    # 0015: реальний відкат на живих даних — вручну, з бекапом).
    op.execute("ALTER TABLE charging_reservations ALTER COLUMN user_id SET NOT NULL;")
    op.execute("ALTER TABLE charging_reservations ALTER COLUMN reserved_kwh SET NOT NULL;")
    op.execute("ALTER TABLE charging_reservations DROP CONSTRAINT IF EXISTS charging_reservations_status_check;")
    op.execute("""
    ALTER TABLE charging_reservations
        ADD CONSTRAINT charging_reservations_status_check
        CHECK (status IN ('pending', 'active', 'finalized', 'cancelled', 'expired'));
    """)
    op.execute("ALTER TABLE charging_reservations DROP CONSTRAINT IF EXISTS charging_reservations_payment_method_check;")
    op.execute("""
    ALTER TABLE charging_reservations
        ADD CONSTRAINT charging_reservations_payment_method_check
        CHECK (payment_method IN ('kwh'));
    """)
