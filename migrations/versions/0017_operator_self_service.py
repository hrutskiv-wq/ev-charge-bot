"""operator_self_service

Revision ID: 0017_operator_self_service
Revises: 0016_charging_reservations
Create Date: 2026-07-28 10:00:00.000000

Самообслуговуваний онбординг оператора — три нові поля на `operators`:

  * `monobank_token_verified_at` (TIMESTAMPTZ, nullable) — НЕ просто "токен
    збережено", а факт реального підтвердження банком (GET
    /api/merchant/details, app/services/monobank_acquiring.py). NULL, поки
    не підтверджено; будь-яке збереження НОВОГО токена
    (set_operator_monobank_token) скидає це поле назад у NULL — стара
    верифікація не має лишатись чинною для нового значення.
  * `has_station` (третій критерій автоактивації) — НЕ окрема колонка,
    рахується з COUNT(operator_stations), нової колонки не потребує.
  * `activated_at` (TIMESTAMPTZ, nullable) — коли оператор вперше перейшов
    у 'active' (автоматично чи вручну через admin_activate_operator).
    Одноразово: наступні suspend/re-activate це поле НЕ чіпають (перше
    значення — історичний факт, а не "поточний статус").
  * `is_public` (BOOLEAN NOT NULL DEFAULT FALSE) — КРИТИЧНА межа безпеки,
    окрема від status. Активний оператор (status='active') ПРИЙМАЄ оплати
    за власним QR — його гроші, його ризик. Але поява станції в
    ПУБЛІЧНОМУ водійському пошуку (list_public_stations_near) — це вже наша
    поверхня (назву/адресу оператор вводить вільним текстом, бачать усі
    водії), і на самообслуговування НЕ віддається: вмикається лише вручну.
    Тому активація (навіть автоматична) НІКОЛИ сама по собі не робить
    станції оператора публічними.

DEFAULT/CHECK нових полів звірені посимвольно з idempotent-дзеркалом у
init_operator_tables() (app/database/operators_repo.py) — той самий клас
розбіжності, що вже стався між міграцією й бутстрапом по kw_transactions
(міграція b1b193e2bd7b), тут навмисно перевірено вручну, бо наявний
schema-drift тест (test_operator_isolation.py) звіряє лише ПРИСУТНІСТЬ
колонок через ALTER TABLE, не їхні DEFAULT/CHECK/тип.

commission_pct: DEFAULT піднято з 4 на 5 (бізнес-рішення 2026-07-23 — 5%,
без абонплати; попередній DEFAULT 4 був розбіжністю з ним). Це стосується
ЛИШЕ НОВИХ операторів — власник явно попросив НЕ бекфілити наявні рядки:
перевірено на проді 2026-07-28 (`SELECT id, name, status, commission_pct
FROM operators`) — рівно один оператор, id=1, commission_pct=4.00,
лишається як є, жодного UPDATE тут немає. DEFAULT сам по собі не міняє вже
записані значення — лише те, що підставляється при НАСТУПНОМУ INSERT без
явного значення.

Три місця, які МАЮТЬ збігатися посимвольно (звірено вручну — наявний
schema-drift тест перевіряє лише присутність колонок, не DEFAULT):
1. Python-параметр `create_operator(commission_pct: float = 5)`
   (app/database/operators_repo.py);
2. `ALTER TABLE operators ALTER COLUMN commission_pct SET DEFAULT 5;` тут;
3. те саме в `init_operator_tables()` (app/database/operators_repo.py).
Ця міграція ще НЕ змержена й НЕ на проді, тож зміна внесена сюди, а не в
окрему 0018.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0017_operator_self_service'
down_revision: Union[str, Sequence[str], None] = '0016_charging_reservations'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TABLE operators ADD COLUMN IF NOT EXISTS monobank_token_verified_at TIMESTAMP WITH TIME ZONE;")
    op.execute("ALTER TABLE operators ADD COLUMN IF NOT EXISTS activated_at TIMESTAMP WITH TIME ZONE;")
    op.execute("ALTER TABLE operators ADD COLUMN IF NOT EXISTS is_public BOOLEAN NOT NULL DEFAULT FALSE;")
    op.execute("ALTER TABLE operators ALTER COLUMN commission_pct SET DEFAULT 5;")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TABLE operators ALTER COLUMN commission_pct SET DEFAULT 4;")
    op.execute("ALTER TABLE operators DROP COLUMN IF EXISTS is_public;")
    op.execute("ALTER TABLE operators DROP COLUMN IF EXISTS activated_at;")
    op.execute("ALTER TABLE operators DROP COLUMN IF EXISTS monobank_token_verified_at;")
