# Контекст проєкту: EV_CHARGE_BOT (eMSP Сервіс)

> Цей документ — коротка "пам'ять" проєкту: поточний стан, архітектурні рішення, стек.
> Онов люйте його після кожної суттєвої зміни архітектури, а не після кожного коміту.
> Останнє оновлення: **2026-07-17**.

## Що це за проєкт

Telegram-бот + HTTP API для мережі зарядних станцій для електромобілів (Україна, бренд **eVolt UA**). Бот дозволяє водію знайти найближчу станцію, поповнити баланс (кВт·год) через Monobank або ваучер, і запустити/зупинити сесію зарядки через протокол **OCPI 2.2.1** (роль **eMSP** — сервіс виступає стороною, що керує водіями та їхніми платежами, а фізичні станції належать CPO-партнеру).

Репозиторій: `github.com/hrutskiv-wq/ev-charge-bot`
Продакшн: Docker Compose на DigitalOcean droplet (`209.38.194.65`, тека `/opt/ev-charge-bot`).

**PR #2 (`fix/security-balance-cleanup`) змержено в `main` 2026-07-17** (commit `38bc6c1`) — усе, що описано нижче в "Поточний стан" і "Наступні кроки", вже в `main`, не лише на фіче-гілці. На сервері досі чекаутнута гілка `fix/security-balance-cleanup` (тепер ідентична `main`) — наступну роботу варто починати новою гілкою від `main`.

## Стек технологій

- **Python 3.11**, `FastAPI` + `uvicorn` — HTTP API (webhooks, OCPI-ендпоінти, PWA).
- **aiogram 3.x** — Telegram-бот; працює через `dp.start_polling()` у фоновому asyncio-таску **всередині того самого FastAPI-процесу** (не окремий процес).
- **asyncpg** напряму (без ORM) + **PostgreSQL 16** — вся робота з БД через сирий SQL у `app/database/`.
- **Alembic** — міграції схеми (`migrations/versions/`).
- **Redis 7** — піднятий у docker-compose, пакет `redis` у залежностях, `REDIS_HOST` прокинутий у env — але **підтверджено: жодного `import redis` в коді немає**. Повністю мертва інфраструктура, доки не переведемо aiogram FSM на `RedisStorage` (див. "Наступні кроки").
- **aiocache** — кешування відповідей Open Charge Map (`app/services/ocm_service.py`).
- **Google Gemini** (`google-genai`) — ініціалізується в `app/core/loader.py` (`ai_client`), використовується в `app/handlers/user.py`.
- **CrewAI + Anthropic Claude** (`ai_agent/`) — окремий, **не інтегрований** у продакшн-застосунок скрипт для AI-асистованої розробки (генерація коду/схем). Не запускається разом з ботом.
- **OCPI 2.2.1** — власна імплементація eMSP-сторони (locations, sessions, tariffs, CDRs, commands).
- **Monobank Acquiring API** — поповнення балансу через webhook з ECDSA-підписом.
- **Open Charge Map API** — пошук найближчих станцій.
- Деплой: **Docker Compose** (основний, підтверджено робочий) або **systemd** (`evolt_bot.service`, альтернативний шлях через `server.py`-шім).

## Архітектура (важливо для швидкого onboarding)

### Один процес, дві ролі
`app/main.py` — єдина точка входу. FastAPI `lifespan` піднімає пул Postgres, ініціалізує таблиці, і запускає `dp.start_polling(bot)` як фоновий asyncio-таск. Тобто HTTP API (OCPI-ендпоінти, Monobank webhook, PWA) і Telegram-бот живуть в **одному** процесі/контейнері `bot`.

`server.py` — це **не** окремий застосунок, а тонкий сумісний шім (`from app.main import app`) для `evolt_bot.service`/`uvicorn server:app`. Уся реальна логіка — в `app/main.py`.

### Bot/Dispatcher — єдиний екземпляр
`app/core/loader.py` створює єдині `bot`/`dp`/`ai_client` на весь застосунок і кастомний `logging.Handler`, що шле ERROR-логи в Telegram-чат (`LOGS_CHAT_ID`), екрануючи HTML і виконуючи мережевий виклик у фоновому тред-пулі (щоб не блокувати event loop). `app/main.py` та хендлери імпортують `bot`/`dp` звідси — не створюйте нових екземплярів `Bot()` деінде.

### Модель балансу (ключове архітектурне рішення)
- `users.balance` (NUMERIC, кВт·год) — кешоване значення, яке показується водію і перевіряється перед стартом зарядки.
- `kw_transactions` — незмінний журнал операцій зі **знаковою** сумою (депозит: `+`, списання: `-`). `SUM(amount)` по користувачу завжди має дорівнювати `users.balance`.
- **Єдина точка запису** — `update_user_balance()` в `app/database/connection.py`. Вона атомарно оновлює і кеш, і журнал в одній транзакції (можна передати вже відкритий `conn`, щоб об'єднати з іншою операцією, як-от запис CDR). **Ніколи не пишіть напряму в `users.balance` або `kw_transactions` з інших модулів.**

### Схема БД: Alembic — джерело правди
`migrations/versions/` — основне джерело схеми. `app/database/connection.py::create_tables()` і `app/database/ocpi_repo.py::init_ocpi_tables()` **дублюють** ту саму схему через `CREATE TABLE IF NOT EXISTS` / `ALTER TABLE ADD COLUMN IF NOT EXISTS`, щоб застосунок піднімався і на чистій БД без ручного запуску `alembic upgrade head`. При зміні схеми — оновлюйте **обидва** місця (нову Alembic-міграцію + відповідний idempotent-блок), інакше вони знову розійдуться.

`billing.sql` — застарілий, **не використовується**, лишений із поясненням чому в самому файлі.

### Безпека зовнішніх ендпоінтів
- `app/api/ocpi.py` (`/ocpi/emsp/2.2.1/*`) — усі вхідні запити від CPO (CDR, callback команд) вимагають заголовок `Authorization: Token <OCPI_SECRET_TOKEN>` (перевіряється через `verify_ocpi_token` dependency). Той самий токен використовує `app/services/ocpi/client.py` для вихідних запитів до CPO.
- `app/api/payments.py` (`/webhook/monobank`) — перевіряє заголовок `x-sign` (ECDSA/SHA-256) проти публічного ключа Monobank (`GET /api/merchant/pubkey`, кешується в пам'яті процесу, потребує `MONOBANK_API_TOKEN`).
- `OCPIConfig` (`app/services/ocpi/config.py`) **навмисно падає при імпорті**, якщо `OCPI_SECRET_TOKEN` не заданий — жодного дефолтного значення.

### Модулі
- `app/services/ocpi/` — `client.py` (HTTP-клієнт до CPO), `commands_service.py` (бізнес-логіка START/STOP сесії з перевіркою балансу), `config.py`.
- `app/services/ocm_service.py` — пошук станцій через Open Charge Map, з кешем.
- `app/handlers/` — aiogram-хендлери (меню користувача, потік зарядки через OCPI-станції).
- `mock_cpo.py` — локальний мок CPO-сервера для тестування OCPI без реального партнера.
- `public/` — статичні файли PWA, віддаються через `StaticFiles` у `app/main.py`.

## Команди розробки

```bash
# Продакшн-залежності
pip install -r requirements.txt
# + dev/test (crewai, pytest) — окремо, НЕ в прод-образ
pip install -r requirements-dev.txt

# Локальний запуск (без Docker)
python -m app.main

# Docker Compose (основний спосіб деплою)
docker compose up -d --build      # новіші версії Docker (без дефіса!)
docker compose logs bot --tail=50
docker compose down

# Міграції
alembic upgrade head
alembic revision -m "опис зміни"

# Тести
pytest
pytest test_ocpi_client.py -v
```

Обов'язкові змінні середовища — див. `.env.example`. Без `OCPI_SECRET_TOKEN` застосунок не стартує; без `MONOBANK_API_TOKEN` — падає верифікація webhook Monobank (перевірка виконується лінива, при першому вхідному webhook).

## Поточний стан (2026-07-17)

Гілка `fix/security-balance-cleanup` виправила і задеплоєна (перевірено на продакшн-сервері, бот піднявся чисто):
- Авторизація OCPI-ендпоінтів і Monobank webhook (раніше були повністю відкритими).
- Розсинхрон балансу між `users.balance` і журналом (три різні місця рахували баланс по-різному).
- Зламаний `server.py` (імпортував неіснуючі функції → ImportError при старті через systemd).
- Три окремі екземпляри `Bot`/`Dispatcher`.
- `.gitignore` у UTF-16 (фактично не працював).
- Хардкоджений пароль Postgres у `docker-compose.yml`.
- Змішані прод/дев залежності, конфлікт версій `crewai`.

## Відомо, потребує уваги

- **Секрети могли потрапити в git-історію** через зламаний `.gitignore` — не перевірено (`git log --all -- .env`). Якщо так — ротувати `BOT_TOKEN`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `OCM_KEY`, `PAYMENT_PROVIDER_TOKEN`.
- **`ai_agent/`** — окремий CrewAI-scaffold, не викликається основним застосунком. Тримати ізольовано (окремий `requirements.txt`, не в `.dockerignore`-виключеннях для прод-образу).
- **Реальна CPO-інтеграція** не перевірена end-to-end — і тепер зрозуміло чому: скрипти, які мали б завантажувати реальні дані від CPO, самі непрацездатні (див. нижче). Поточне тестування йде проти `mock_cpo.py`.
- **`sync.py` / `sync_locations.py` / `sync_commercial.py` — знайдено і виправлено (2026-07-17).**
  - `sync.py` писав станції в окремий SQLite-файл (`users.db`), не пов'язаний з реальною PostgreSQL-базою застосунку — залишок доpostgres-версії бота. **Виправлено**: тепер пише через `save_station_to_local_db()` в ту саму Postgres, якою живе бот.
  - `sync_locations.py` і `sync_commercial.py` викликали async-функції (`init_ocpi_tables`, `save_ocpi_location/tariff/session`) **без `await`** (корутини створювались і одразу губились — фактично нічого не записувалось), викликали неіснуючі методи `OCPIClient.get_locations/get_tariffs/get_sessions`, і в кінці "перевіряли результат" читанням з того ж стороннього SQLite-файлу, а не з Postgres. **Виправлено**: додано реальні методи в `OCPIClient`, розставлено `await`, перевірка результату тепер читає з Postgres через `get_db_pool()`.
- **Інцидент 2026-07-17: `.env` без завершального переносу рядка зламав `docker compose up`.** Дописування нових змінних (`POSTGRES_USER/PASSWORD/DB`, `OCPI_SECRET_TOKEN`) через `cat >> .env << 'EOF'` злиплося з попереднім останнім рядком файлу (`LOGS_CHAT_ID=...POSTGRES_USER=...` в один рядок), бо старий `.env` не закінчувався `\n`. Наслідок: `docker compose` не бачив `POSTGRES_USER` при інтерполяції `${POSTGRES_USER}` у `DB_URL` (`environment:` секція) — конекшн-стрінг виходив із порожнім юзером, і `asyncpg` намагався підключитись як ОС-юзер контейнера (`root`) → `InvalidPasswordError`. **Виправлено** через `sed` (вставлено перенос рядка перед злиплим значенням). **Урок**: перед будь-яким `cat >> .env` перевіряти `tail -c1 .env | xxd` (чи файл закінчується `\n`), або просто відкривати файл у редакторі замість дописування наосліп.

## Відомо, потребує уваги (продовження)

- ⚠️ **`GEMINI_API_KEY` не працює — обмеження на стороні Google, не наш баг (виявлено 2026-07-17).** У логах (`eVolt UA: Logs`) регулярно з'являється `401 UNAUTHENTICATED` / `ACCESS_TOKEN_TYPE_UNSUPPORTED` при виклику Gemini API (`app/core/loader.py::ai_client = genai.Client(api_key=...)`). Діагностика: змінна `GEMINI_API_KEY` присутня в контейнері (не пропала), формат ключа (`AQ.Ab8RN6...`) коректний для акаунтів, створених у 2026 — Google перевів видачу ключів у Google AI Studio зі старого формату `AIza...` на новий `AQ.Ab...`. Перевірено і виключено: (1) SDK-специфічна проблема (Vertex AI auto-switch через env-змінні `GOOGLE_GENAI_USE_VERTEXAI`/`GOOGLE_CLOUD_PROJECT` — відсутні в контейнері), (2) баг форматування самого ключа (без пробілів/лапок). Прямий HTTP-запит до `generativelanguage.googleapis.com` з ключем як query-параметром (в обхід SDK) дає ту саму помилку — це підтверджує, що це не проблема нашого коду чи бібліотеки `google-genai`, а реальне обмеження на рівні акаунта/проєкту Google: з 19 червня 2026 Gemini API почав відхиляти необмежені Standard-ключі (`AIza`), з вересня 2026 їх буде відхилено повністю, а нові `AQ.`-ключі з AI Studio для багатьох акаунтів (включно з нашим) поки що не працюють при реальних викликах — масова, ще не вирішена проблема (десятки скарг на форумі Google AI Developers, напр. https://discuss.ai.google.dev/t/account-restricted-to-aq-prefix-keys-requesting-standard-aiza-key-restoration-1/174580). **Статус: не вирішено, чекаємо фіксу від Google або пробуємо відновити `AIza`-ключ через форму зворотного зв'язку**. Функціонал оплат/заряджання від цього не залежить — лише опціональна ШІ-функція бота.

- ✅ **`ocpi_emsp_cdrs_refactored.py` — вирішено: ідею доінтегровано, чернетку видалено (2026-07-17).** Корінь репо містив окремий осиротілий файл з власними `receive_cdr`/`CDRRequest` (Pydantic-модель), ніде не імпортований жодним реальним модулем застосунку — мертвий код з двома багами (списання за `total_cost` замість `total_energy`, прямий запис у `kw_transactions` в обхід `update_user_balance()`). Користувач обрав: доінтегрувати як Pydantic-модель. **Зроблено**: сама ідея типізації винесена в новий `app/schemas/ocpi.py::CDRRequest` (без багів бізнес-логіки, з докладним поясненням чому саме без них), `app/api/ocpi.py::receive_cdr` тепер приймає `cdr: CDRRequest` замість `dict` — валідація полів (непорожні id/session_id, `auth_id > 0`, `total_energy`/`total_cost >= 0`) відбувається на рівні моделі, FastAPI сам повертає 422. Це заразом закриває п. 6 нижче. Осиротілі `ocpi_emsp_cdrs_refactored.py` і `test_ocpi.py` (тестував саме мертвий файл, а не продакшн-код) видалені з репозиторію; `test_ocpi_cdr.py` переписано під нову сигнатуру — тести на успіх/ідемпотентність дубліката/500 без пулу викликають реальну функцію з `CDRRequest(...)`, а тести на "погані" дані (від'ємні значення, відсутні поля, порожні рядки, `auth_id <= 0`) тепер перевіряють `pydantic.ValidationError` безпосередньо на моделі — оскільки за нової архітектури невалідний `CDRRequest` неможливо навіть побудувати, а не те що передати у функцію. `pytest.ini` більше не виключає `test_ocpi.py` (файла не існує).

## Наступні кроки (рекомендації, за пріоритетом)

1. ✅ **Тести на щойно виправлене — написано (2026-07-17).** Нові файли в корені репо: `conftest.py` (виставляє `OCPI_SECRET_TOKEN` до імпорту тестових модулів), `pytest.ini` (`asyncio_mode = auto`), `test_ocpi_auth.py` (401 без токена / з неправильним токеном / без префікса `Token ` / успіх з правильним), `test_balance.py` (депозит пишеться `+`, списання `-`, обидва запити йдуть через один `conn` — на фейковому з'єднанні, без реальної Postgres), `test_monobank_signature.py` (валідний підпис приймається, підмінене тіло і підпис чужим ключем — відхиляються, на власній тестовій ECDSA-парі, без залежності від живого API Monobank). Логіку ECDSA-перевірки прогнано ізольовано через `cryptography` — коректна. Повний прогін через `pytest` ще не виконувався в CI/на сервері (в пісочниці розробки немає мережі для встановлення `fastapi`/`asyncpg`/`httpx`) — **перед мержем виконати на сервері**: `pip install -r requirements-dev.txt && pytest -v`.
2. ✅ **CI — додано (2026-07-17).** `.github/workflows/ci.yml`: на кожен push і PR в `main` встановлює `requirements.txt` + `requirements-dev.txt` на Python 3.11 і ганяє `pytest -v`. Postgres/Redis-сервіс не потрібен — усі тести мокають БД і мережу. Потребує лише `OCPI_SECRET_TOKEN` (плейсхолдер, не секрет) в env кроку — реальні секрети (`BOT_TOKEN`, `OCM_KEY` тощо) в CI не потрібні й не додавались. **Одразу ж виявив реальний конфлікт залежностей** при відкритті PR: `pip install -r requirements.txt -r requirements-dev.txt` в одній команді провалювався — `aiofiles==25.1.0` (requirements.txt) конфліктував з `crewai==1.15.2`, який жорстко вимагає `aiofiles~=24.1.0`. На сервері це не проявлялось, бо `requirements-dev.txt` ставили ОКРЕМОЮ командою поверх вже зібраного образу — pip у такому разі просто тихо занижував aiofiles до 24.1.0 без помилки. **Виправлено**: пін у `requirements.txt` змінено на `aiofiles~=24.1.0` (сумісно і з aiogram, і з crewai).
3. ✅ **FSM-стан бота в Redis — зроблено (2026-07-17).** `app/core/loader.py`: якщо заданий `REDIS_HOST` — `RedisStorage.from_url(...)`, інакше `MemoryStorage()` (для локальної розробки без Redis). `app/main.py` тепер викликає `await dp.storage.close()` при shutdown. Тест вибору сховища — `test_fsm_storage.py` (перезавантажує модуль з різними env, без потреби живого Redis). На проді `REDIS_HOST=redis` вже проброшений у `docker-compose.yml`, тож після деплою й рестарту контейнера має одразу піти в Redis.
4. ✅ **Health-check ендпоінт — додано (2026-07-17).** `GET /health` в `app/main.py`: перевіряє `SELECT 1` на Postgres (обов'язково) і `PING` на Redis (лише якщо FSM реально на `RedisStorage`, для `MemoryStorage` не критично) — 200 якщо все ок, 503 якщо ні, з деталями по кожному компоненту в JSON. `docker-compose.yml`: додано `healthcheck` для `postgres` (`pg_isready`), `redis` (`redis-cli ping`) і `bot` (python+urllib, без curl в образі), `bot` тепер залежить від `postgres`/`redis` через `condition: service_healthy`, а не просто порядок запуску. Тести — `test_health.py` (моки `db_pool`/`dp.storage.redis`, живі Postgres/Redis не потрібні).
5. ✅ **Автоматичні бекапи Postgres — додано (2026-07-17).** `scripts/backup_postgres.sh`: `pg_dump` через `docker compose exec postgres`, gzip, ротація (за замовчуванням 14 днів, `BACKUP_RETENTION_DAYS`). **Свідомий вибір**: бекапи поки локальні на самому сервері (`backups/`, у `.gitignore`) — захищає від поганої міграції/помилкового запиту, але НЕ від втрати самого сервера/диска. Винесення на зовнішнє сховище (DigitalOcean Spaces/S3) відкладено — рішення користувача, коли буде обрано сховище; скрипт спроєктовано так, що додати `rclone`/`aws s3 cp` виклик в кінці — одна дописана команда, без зміни решти логіки. Команда для cron і команда відновлення — в коментарях самого скрипта.
6. **Pydantic-моделі для OCPI-пейлоадів.** Зараз `receive_cdr(cdr: dict)` приймає нетипізований dict — типізація дасть валідацію "з коробки" і документацію в Swagger. Заразом перевірити, чи `/docs` не публічний у продакшні (там видно всю структуру внутрішнього API).
7. ✅ **Реконсиляція платежів — додано (2026-07-17), заразом знайдено і виправлено третій "тіньовий" шлях запису балансу.** `app/handlers/user.py::process_successful_payment` (оплата через Telegram Invoice, не Monobank) писав напряму `UPDATE users SET balance ...` + `INSERT INTO kw_transactions`, в обхід і `update_user_balance()`, і таблиці `payments` — платіж через Telegram взагалі не залишав сліду в `payments` (жодного `invoice_id`, суми в грн, статусу). Це саме той клас бага, що й раніше виправлявся для OCPI CDR і Monobank webhook, тут просто не помічений одразу. **Виправлено**: тепер спершу пишеться `payments` (provider='telegram', invoice_id=`telegram_payment_charge_id` — унікальний і незмінний від Telegram), потім нарахування йде через `update_user_balance()` з `payment_id`. Новий `reconcile_payments.py --days N`: звіряє `payments` (status='success') з `kw_transactions` за трьома ознаками — оплачено-не-нараховано, нараховано-без-підтвердженого-платежу, розбіжність суми (з урахуванням двох фіксованих пакетів 750→50 кВт·год і 1350→100 кВт·год зі знижкою, а не наївного ділення на `PRICE_PER_KWH`). Exit code 1 при розбіжностях — придатний для cron+алертів. Тести: `test_reconcile_payments.py`, `test_telegram_payment.py`.
8. ✅ **`refund` — доведено до робочого стану (2026-07-17).** Виявлено реальне розходження схеми: `migrations/versions/b1b193e2bd7b_initial_schema.py` (Alembic, джерело правди) НІКОЛИ не мав `'refund'` в enum `transaction_type` — лише idempotent-бутстрап у `connection.py::create_tables()` вже давно містив `'refund'`, але той блок обгорнутий у `IF NOT EXISTS (SELECT 1 FROM pg_type ...)` і не чіпає вже існуючий тип, тож на прод-базі `'refund'` насправді був відсутній. Додано `migrations/versions/0008_add_refund_transaction_type.py` (`ALTER TYPE ... ADD VALUE IF NOT EXISTS 'refund'`) — **потребує `alembic upgrade head` на сервері**. Також виявлено, що `update_user_balance()` завжди хардкодив тип у журналі як `'deposit'`/`'withdrawal'`, ігноруючи реальний `t_type` — тепер `t_type="refund"` коректно записує кредит з типом `'refund'` (не `'deposit'`) в `kw_transactions`. Новий адмін-скрипт `refund_transaction.py <user_id> <amount_kwh> ["опис"]` (за зразком `inject_deposit.py`). Рефанд лишається ручною дією підтримки/адміна — автоматичного тригера немає навмисно (CDR сам по собі не сигналізує однозначно про невдалу фізичну сесію). Тест — `test_balance.py::test_refund_increases_balance_and_stores_refund_type_not_deposit`.
