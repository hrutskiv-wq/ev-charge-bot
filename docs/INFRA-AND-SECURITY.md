# Інфраструктура та безпека (рантайм-довідник)

> Стан на 2026-07-22. Сервер: DigitalOcean droplet, IP `209.38.194.65`,
> стек `docker compose` (bot + postgres + redis), каталог `/opt/ev-charge-bot`.
> Цей файл — довідник з рантайму й безпеки: домен, HTTPS, мережа, секрети.
> Продуктова/фіче-документація — в інших файлах (`SESSION_STATE.md`,
> `kwh-wallet-design.md` тощо).

## 1. Домен і HTTPS

- **Домен:** `chargebot.com.ua` (реєстратор NIC.UA, сервери імен
  `ns10/11/12.uadns.com`, дійсний до 22 жовтня 2026).
- **DNS:** два A-записи в панелі NIC.UA — `@` і `www` → `209.38.194.65`.
- **Reverse proxy:** nginx на сервері (порти 80/443). Конфіг:
  `/etc/nginx/sites-available/chargebot` (симлінк у `sites-enabled/`),
  `proxy_pass http://127.0.0.1:8000` з проксі-заголовками
  (`Host`, `X-Real-IP`, `X-Forwarded-For`, `X-Forwarded-Proto`).
- **TLS:** Let's Encrypt через `certbot --nginx`, сертифікат на
  `chargebot.com.ua` + `www.chargebot.com.ua`, чинний до 2026-10-20.
  Автопродовження налаштоване certbot'ом (systemd timer). Редирект
  http → https увімкнено (301).
- **`PUBLIC_BASE_URL=https://chargebot.com.ua`** (в `.env`). Використовується
  для QR-сторінок станцій (`/s/{slug}`) і для URL Monobank-вебхуків.
- Старий домен `botcharge.com.ua` — не використовується (можна дати
  доспливти; на сервер більше не вказує в конфігах).

### Корисні команди
```bash
certbot certificates          # переглянути наявні сертифікати й терміни
certbot renew --dry-run       # перевірити, що автопродовження працює
nginx -t && systemctl reload nginx
dig +short chargebot.com.ua   # має віддати 209.38.194.65
curl -s https://chargebot.com.ua/health   # {"status":"ok",...}
```

### Друк QR-наліпок
Генеруй QR станції з кабінету оператора **після** встановлення
`PUBLIC_BASE_URL=https://chargebot.com.ua` — тоді код веде на
`https://chargebot.com.ua/s/{slug}` (постійна адреса, не міняється).

## 2. Мережа / порти (`docker-compose.yml`)

Усі опубліковані порти прив'язані до `127.0.0.1` (НЕ `0.0.0.0`), тобто
недоступні з інтернету:

| Сервіс   | Публікація              | Навіщо                              |
|----------|-------------------------|-------------------------------------|
| bot      | `127.0.0.1:8000:8000`   | за ним nginx (https)                |
| postgres | `127.0.0.1:5432:5432`   | лише локально / SSH-тунель          |
| redis    | `127.0.0.1:6379:6379`   | лише локально                       |

- Бот ходить до бази й redis через **внутрішню docker-мережу** за іменами
  (`postgres:5432`, `redis:6379`), а не через опубліковані порти.
- Ручні psql-команди — через `docker compose exec postgres ...`
  (не потребують опублікованого порту).
- Комміт зі зміною: `19a0965c` «security: bind postgres/redis/bot ports to
  localhost, drop obsolete version».

## 3. Секрети (`.env`)

**`.env` НЕ відстежується git** (перевірено `git ls-files`). Секрети живуть
лише на сервері у `/opt/ev-charge-bot/.env`. У репо — тільки `.env.example`
з назвами змінних без значень.

### Статус ротації (2026-07-22)

| Змінна                   | Що це                                    | Де ротувати                              | Статус |
|--------------------------|------------------------------------------|------------------------------------------|--------|
| `BOT_TOKEN`              | токен Telegram-бота                      | @BotFather → `/revoke`                    | ✅ ротовано |
| `POSTGRES_PASSWORD`      | пароль БД                                | `ALTER USER` + `.env` (див. нижче)        | ✅ ротовано |
| `GEMINI_API_KEY`         | Google Gemini (AI-чат)                   | aistudio.google.com/app/apikey            | ✅ ротовано |
| `OCM_KEY`                | OpenChargeMap (пошук станцій)            | профіль OCM → застосунки (новий → старий видалити) | ✅ ротовано |
| `OCPI_SECRET_TOKEN`      | внутрішній секрет OCPI                    | локально: `openssl rand -hex 32`          | ✅ ротовано |
| `PAYMENT_PROVIDER_TOKEN` | Telegram Payments (**тестовий**)         | BotFather → Payments                      | тестовий — ротація не потрібна |
| `ENCRYPTION_KEY`         | Fernet, шифрує токен оператора Monobank у БД | **НЕ ротувати наосліп** (див. нижче)   | лишено (не був публічним) |
| `POSTGRES_DB`, `POSTGRES_USER`, `LOGS_CHAT_ID`, `PUBLIC_BASE_URL` | не секрети | — | — |

### Процедура ротації `POSTGRES_PASSWORD`
Пароль зберігається в томі `pgdata`, тому мало змінити `.env` — треба ще
`ALTER USER` у самій базі. Блок робить обидва синхронно, новий пароль ніде
не друкується (бот читає його з `.env`):
```bash
cd /opt/ev-charge-bot
NEWPG=$(openssl rand -hex 24)
docker compose exec -T -e NEWPG="$NEWPG" postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v pw="$NEWPG" -c "ALTER USER CURRENT_USER PASSWORD :'\''pw'\'';"' \
  && sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${NEWPG}|" .env \
  && docker compose up -d --force-recreate bot
# перевірка: docker compose logs bot | grep "Пул підключень"
```

### `ENCRYPTION_KEY` — обережно
Цей ключ шифрує `operators.monobank_token_encrypted`. Якщо його змінити,
наявний зашифрований токен оператора стане **нечитабельним** і оплата на
станціях зламається. Ротувати можна ЛИШЕ так: згенерувати новий ключ →
оновити `.env` → перезапустити бот → оператор **заново під'єднує токен
Monobank через бота** (перешифрування новим ключем). Наразі лишено без змін
(ключ ніколи не був у git і публічно не показувався).

### Загальний патерн ротації решти
1. Перевипустити значення у провайдера (BotFather / Google / OCM…).
2. `nano .env` → замінити значення відповідної змінної.
3. `docker compose up -d --force-recreate bot`.
4. Перевірити відповідну функцію (бот відповідає / пошук станцій / AI-чат).

## 4. Історія git і старі секрети
Колись пароль Postgres міг бути хардкоднутий у `docker-compose.yml` і
потрапити в історію публічного репо. Оскільки всі живі секрети ротовані,
старі значення в історії тепер марні. Чистка історії (`git filter-repo`) —
**необов'язкова й низькоцінна** (репо публічне: форки/кеші не приберуться).

## 5. Відкриті пункти (беклог рантайму)
- **Перезавантаження сервера** через оновлення ядра (некритично). Після
  ребута перевірити `docker compose ps` — усі три контейнери мають піднятись
  автоматично (`restart: always`).
- **Реальні оплати за кВт (фаза 6a kWh-гаманця)** — див. `kwh-wallet-design.md`.
  Передумови: юридичний договір/фіскалізація, готовий Промпт 5 (звірка).
  Технічна розвилка: перевикористати Monobank-еквайринг (вже працює) замість
  заведення живого Telegram Payments провайдера.
