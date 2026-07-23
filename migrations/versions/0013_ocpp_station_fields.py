"""ocpp_station_fields

Revision ID: 0013_ocpp_station_fields
Revises: 0012_wallet_topups
Create Date: 2026-07-23 16:00:00.000000

OCPP Промпт 3a — кістяк Central System. Три нові поля на operator_stations,
через ALTER TABLE (не через переписування CREATE TABLE 0010), той самий
патерн, що вже й у kw_transactions.session_id (app/database/connection.py):

  * ocpp_auth_key_encrypted TEXT — Fernet-шифрований спільний секрет для
    OCPP Basic Auth (security profile 1), той самий ENCRYPTION_KEY, що й
    operators.monobank_token_encrypted. НЕ входить у _STATION_FIELDS
    (app/database/operators_repo.py) — секрет, дістається лише окремою
    dedicated-функцією get_station_ocpp_auth_key_encrypted(), той самий
    принцип, що й get_operator_monobank_token_encrypted().
  * ocpp_status VARCHAR(20) — останній статус з StatusNotification. Без
    CHECK: це enum зовнішнього протоколу (ocpp.v16.enums.ChargePointStatus),
    не наш власний — жорстке обмеження зламало б прийом легітимних значень,
    яких ми не передбачили.
  * ocpp_last_seen_at TIMESTAMPTZ — оновлюється при КОЖНОМУ прийнятому
    OCPP-повідомленні (BootNotification/Heartbeat/StatusNotification), не
    лише при статусі — інакше "мовчазна" станція, що шле лише Heartbeat,
    виглядала б навіки не баченою.

Обидва нові поля НЕ секрети — додані в _STATION_FIELDS (спільна константа,
яку читає й публічний водійський пошук list_public_stations_near) свідомо,
на відміну від auth-ключа.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0013_ocpp_station_fields'
down_revision: Union[str, Sequence[str], None] = '0012_wallet_topups'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TABLE operator_stations ADD COLUMN IF NOT EXISTS ocpp_auth_key_encrypted TEXT;")
    op.execute("ALTER TABLE operator_stations ADD COLUMN IF NOT EXISTS ocpp_status VARCHAR(20);")
    op.execute("ALTER TABLE operator_stations ADD COLUMN IF NOT EXISTS ocpp_last_seen_at TIMESTAMP WITH TIME ZONE;")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TABLE operator_stations DROP COLUMN IF EXISTS ocpp_last_seen_at;")
    op.execute("ALTER TABLE operator_stations DROP COLUMN IF EXISTS ocpp_status;")
    op.execute("ALTER TABLE operator_stations DROP COLUMN IF EXISTS ocpp_auth_key_encrypted;")
