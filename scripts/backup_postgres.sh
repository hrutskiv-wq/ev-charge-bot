#!/usr/bin/env bash
set -euo pipefail

# Автоматичний щоденний бекап PostgreSQL для eVolt UA.
#
# Це фінансова система (users.balance, kw_transactions, платежі Monobank) —
# до цього скрипта бекапів не було взагалі. Дамп зберігається ЛОКАЛЬНО на
# цьому ж сервері (backups/) — це захищає від пошкодженої міграції чи
# помилкового UPDATE/DELETE, але НЕ від втрати самого сервера чи диска.
# Якщо з'явиться зовнішнє сховище (DigitalOcean Spaces, S3 тощо) — досить
# дописати один виклик rclone/aws s3 cp в кінці цього файлу після рядка
# з ротацією; сам дамп і ротацію міняти не треба.
#
# Встановлення в cron (щодня о 3:00 ночі):
#   crontab -e
#   0 3 * * * /opt/ev-charge-bot/scripts/backup_postgres.sh >> /var/log/ev-charge-bot-backup.log 2>&1
#
# Відновлення з бекапу (УВАГА: перезаписує поточну базу):
#   gunzip -c backups/ev_charge_bot_2026-07-17_030000.sql.gz | \
#     docker compose exec -T postgres psql -U "$POSTGRES_USER" "$POSTGRES_DB"

PROJECT_DIR="/opt/ev-charge-bot"
BACKUP_DIR="$PROJECT_DIR/backups"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
TIMESTAMP=$(date +%Y-%m-%d_%H%M%S)
DUMP_FILE="$BACKUP_DIR/ev_charge_bot_${TIMESTAMP}.sql.gz"

mkdir -p "$BACKUP_DIR"
cd "$PROJECT_DIR"

echo "[$(date -Iseconds)] Починаємо бекап PostgreSQL -> $DUMP_FILE"

# POSTGRES_USER/POSTGRES_DB беремо з env самого контейнера postgres (вони
# вже там прокинуті через docker-compose.yml) — скрипту не треба самому
# парсити .env і дублювати ці значення.
if docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' | gzip > "$DUMP_FILE"; then
    SIZE=$(du -h "$DUMP_FILE" | cut -f1)
    echo "[$(date -Iseconds)] ✅ Бекап успішний: $DUMP_FILE ($SIZE)"
else
    echo "[$(date -Iseconds)] ❌ Бекап ПРОВАЛИВСЯ! Видаляю неповний файл."
    rm -f "$DUMP_FILE"
    exit 1
fi

# Ротація: видаляємо дампи старіші за RETENTION_DAYS днів (за замовчуванням 14).
DELETED_COUNT=$(find "$BACKUP_DIR" -name "ev_charge_bot_*.sql.gz" -mtime "+${RETENTION_DAYS}" -print -delete | wc -l)
echo "[$(date -Iseconds)] Ротація: видалено ${DELETED_COUNT} старих бекапів (>${RETENTION_DAYS} днів)."
