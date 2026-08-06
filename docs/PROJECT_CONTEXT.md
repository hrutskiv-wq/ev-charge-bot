# Контекст проєкту: ev-charge-bot (eVolt UA)

> Технічна пам'ять репозиторію. Читається **перед будь-яким кодом**.
> Оновлювати після суттєвих змін архітектури, не після кожного коміту.
>
> **Переписано 06.08.2026 повністю за фактичним станом коду.** Попередня редакція
> (29.07.2026) описувала роль eMSP за OCPI 2.2.1 і не містила жодної згадки OCPP —
> тобто описувала етап, що закінчився 17.07.2026. Усі твердження нижче звірені
> з кодом; посилання дано у форматі `файл:рядок`.

---

## 1. Що це за продукт насправді

**CPO-платформа**: власник зарядної точки під'єднує своє залізо до нашої центральної
системи по **OCPP 1.6J**, а водій платить карткою без встановлення застосунку.
Гроші йдуть **напряму на мерчант-рахунок оператора** — платформа коштів не торкається.

Три способи, якими водій починає зарядку:

1. **QR на станції → веб** (`app/api/driver_qr.py`) — без Telegram, без реєстрації
2. **Telegram-бот** — пошук станції, kWh-гаманець, ваучери
3. **Адмінські команди** `/ocpp_start`, `/ocpp_start_uah` (`app/handlers/ocpp_admin.py`)

**Кабінет оператора — тільки в Telegram** (`app/handlers/operator_billing.py`, 1230 рядків).
Вебчастини для оператора немає. Вебфлоу існує лише для водія.

Чого в коді **немає**, попри згадки в бізнес-документах: **ПРРО / Checkbox** — жодного
рядка, рішення зафіксоване тільки в `CLAUDE.md` §5. Фіскалізації в продукті наразі нема.

---

## 2. Точки входу й процеси

**Один процес.** `Dockerfile` → `python -m app.main` → `uvicorn.run(app, 0.0.0.0:8000)`
(`app/main.py:229`). `evolt_bot.service` → `uvicorn app.main:app`. Telegram-полінг
живе всередині того самого процесу як asyncio-таска (`app/main.py:74`).

`server.py` — шім у 18 рядків: `from app.main import app`. Не окремий застосунок.

`docker-compose.yml`: три сервіси — `bot`, `redis:7-alpine`, `postgres:16-alpine`,
порти прив'язані до `127.0.0.1`, healthcheck через `/health`.

**Наслідок одного процесу, який визначає архітектуру:** реєстр живих OCPP-з'єднань
`_active_charge_points` (`app/api/ocpp_ws.py:110`) тримається **в пам'яті**. Тому
RemoteStart з окремого процесу неможливий — це прямо задокументовано в
`start_charging_session.py` і враховано в логіці звірок.

**Startup** (`app/main.py:47-75`): `init_postgres()` → `init_ocpi_tables()` →
`init_operator_tables()` → `warn_if_key_missing()` → `bot.set_my_commands([6 команд])` →
`asyncio.create_task(dp.start_polling(bot))`.

**Bot і Dispatcher — єдиний екземпляр** з `app/core/loader.py:92` і `:116`.
Ніде більше не створювати. FSM-сховище — `RedisStorage` при заданому `REDIS_HOST`,
інакше `MemoryStorage` з WARNING (`loader.py:103-114`).

**Swagger вимкнений за замовчуванням**: `docs_url`/`redoc_url`/`openapi_url` — `None`,
вмикаються тільки `ENABLE_API_DOCS=1` (`app/main.py:104`). У проді не вмикати.

---

## 3. HTTP-поверхня — повний перелік

| файл:рядок | метод | шлях | авторизація |
|---|---|---|---|
| `app/main.py:167` | GET | `/health` | немає (пінг Postgres + Redis) |
| `app/main.py:211` | GET | `/api/stations?lat&lon` | немає |
| `app/main.py:219` | GET | `/pwa` | немає |
| `app/main.py:226` | mount | `/` → `public/` | немає |
| `app/api/driver_qr.py:93` | GET | `/s/{qr_slug}` | slug сам є ключем |
| `app/api/driver_qr.py:129` | POST | `/s/{qr_slug}/start` | немає; `operator_id` ніколи не приймається ззовні |
| `app/api/driver_qr.py:237` | GET | `/s/{qr_slug}/receipt/{session_id}` | перевірка `session.station_id == station.id` (`:247`) |
| `app/api/operator_webhook.py:172` | POST | `/webhook/operator/{operator_id}` | підпису немає — **анти-оракул**, див. нижче |
| `app/api/wallet_webhook.py:222` | POST | `/webhook/wallet/{operator_id}` | те саме |
| `app/api/charging_hold_webhook.py:143` | POST | `/webhook/charging-hold/{operator_id}` | те саме + вимога статусу рівно `hold` (`:196`) |
| `app/api/ocpp_ws.py:461` | **WS** | `/ocpp/{cp_id}` | підпротокол `ocpp1.6` + станція `mode='ocpp'` + оператор `active` + HTTP Basic (пароль = Fernet-розшифрований `ocpp_auth_key_encrypted`, `hmac.compare_digest`) |
| `app/api/ocpi.py:35` | POST | `/ocpi/emsp/2.2.1/cdrs` | `Authorization: Token <OCPI_SECRET_TOKEN>` — **законсервовано, див. §11** |
| `app/api/ocpi.py:107` | POST | `.../callback/commands/START_SESSION/{user_id}` | те саме |
| `app/api/ocpi.py:149` | POST | `.../callback/commands/STOP_SESSION/{user_id}` | те саме |

**Модель безпеки вебхуків (не міняти, не розуміючи).** Тіло вебхука **не є джерелом
правди**: з нього береться лише `invoiceId` як ключ, а справжній статус перепитується
в банку токеном оператора (`operator_webhook.py:216`). Невідомий invoice → **тихий 200**,
щоб ендпоінт не працював оракулом для перебору. Саме тому підпису немає — і саме тому
його відсутність не є дірою.

WS-автентифікація навпаки: **усі відмови повертають однаковий код 1008 без причини** —
щоб не давати різницю між «немає станції», «оператор неактивний» і «пароль не той».

---

## 4. Telegram-частина

Роутери aiogram, **порядок значущий** (`app/main.py:154-158`):

| # | роутер | що ловить |
|---|---|---|
| 154 | `bot_stations_router` (`ocpi_stations.py`) | `/ocpi` + callback `ocpi_*` — законсервовано, §11 |
| 155 | `operator_billing_router` | `/operator`, `🏷️ Мій білінг`, майстри, `opm:*`/`opst:*`/`opadm:*`/`oprev:*`/`opcsv:*` |
| 156 | `ocpp_admin_router` | `/ocpp_start`, `/ocpp_stop`, `/ocpp_start_uah` |
| 157 | `user_router` | `/start`, `/balance`, `/charge`, `/voucher`, `/support`, `/history`, локація, голос, платежі |
| 158 | `charge_router` | **недосяжний**, §12 |

Причина порядку: `user.py:1216` — catch-all `lambda m: m.text and not m.text.startswith('/')`
зі `StateFilter("*")` ковтає будь-який вільний текст і віддає його ШІ-чату. Усе, що має
спрацювати раніше, реєструється **вище** за `user_router`.

**Меню бота** (`app/main.py:64-71`): `/start`, `/balance`, `/charge`, `/voucher`,
`/support`, `/operator`. Команди `/ocpi`, `/ocpp_start*` у меню немає, але набрати їх може будь-хто.

**FSM-стани.** `app/states/operator_states.py`: `OperatorOnboarding`, `MonobankConnect`,
`StationWizard` (7 кроків), `TariffEdit`. Але `BotStates` водія визначені **прямо в
хендлері** (`user.py:138`), а не в `app/states/` — розбіжність конвенції.

**Адмін-гейт** — `_is_from_admin_chat()` (`operator_billing.py:297`): `chat_id == LOGS_CHAT_ID`.
Тобто чат логів **водночас є адмін-чатом**. Змінюючи `LOGS_CHAT_ID`, ви змінюєте права.

---

## 5. OCPP 1.6J — ядро продукту

`app/api/ocpp_ws.py` (541 рядок), бібліотека `ocpp==2.1.0`. Адаптер
`_StarletteWebSocketAdapter` (`:113`) перекладає `.recv()/.send()` ↔ Starlette.

Оброблювані повідомлення: `BootNotification` (`:167`), `Heartbeat` (`:178`),
`StatusNotification` (`:185`), `Authorize` (`:192`), `StartTransaction` (`:200`),
`StopTransaction` (`:260`), `MeterValues` (`:379`). Решта отримує `NotImplemented`
від самої бібліотеки.

**Дві свідомі заглушки, про які треба знати:**

- `Authorize` **завжди повертає `Accepted`** — перевірки балансу на цьому кроці немає.
  Гроші тримаються резервацією, а не відмовою в авторизації
- `MeterValues` — **лише телеметрія, не джерело білінгу.** Білінг рахується з дельти
  `meter_start_wh` / `meter_stop_wh` у `StopTransaction`, з капом
  `MAX_REASONABLE_SESSION_WH = 500_000` (`:106`); абсурдна дельта → `kwh = None` → сума 0

**Окремої таблиці транзакцій немає.** OCPP-транзакція живе в `operator_sessions`
(`ocpp_transaction_id`, `meter_start_wh`, `meter_stop_wh`, міграція 0014), а
`transaction_id` для станції = `id` сесії. Два часткові унікальні індекси
(`operators_repo.py:246-253`) не дають ані дубля transaction_id, ані двох активних
сесій на одній станції.

**Зв'язок із грошима — через `charging_reservations`**, ключ `id_tag`.
`_try_activate_reservation()` (`:234`) прив'язує резервацію на `StartTransaction`.
На `StopTransaction` — розгалуження за `payment_method`:

- `'kwh'` (**модель A**) → `complete_ocpp_transaction_and_release()` (`:302`), одна атомарна транзакція
- `'uah'` (**модель B**) → `_settle_uah_reservation()` (`:324`), лінивий імпорт
  `app.services.ocpp_charging` через циклічну залежність (коментар `:329-334`)
- немає резервації → `complete_ocpp_transaction()` без грошей

---

## 6. Гроші

### 6.1. Єдина точка запису

`update_user_balance()` — `app/database/connection.py:179-302`. Гілки за `t_type`:

| гілка | рядок | поведінка |
|---|---|---|
| `deposit` / `monobank_jar` / `refund` | `:232` | кредит |
| `release` | `:251` | кредит, ledger-тип `release` |
| `hold` | `:265` | дебет із `WHERE balance >= $1`; **повертає `False`**, якщо не вистачило, і нічого не пише |
| інше (напр. `ocpi_session`) | `:282` | дебет **без** захисту від мінуса |

Знак у журналі: депозит `+`, списання `−`. Інваріант:
`SUM(kw_transactions.amount)` по користувачу **завжди** дорівнює `users.balance`.

### 6.2. Хто пише правильно

`app/api/ocpi.py:88`, `app/api/wallet_webhook.py:126`, `app/handlers/user.py:1093`
(Telegram Payments), `operators_repo.py:1030` (`hold`), `:1145` (`release` залишку),
`:1230` (`release` повного холду), `inject_deposit.py:20`, `refund_transaction.py:27`.

### 6.3. Хто пише в обхід — відкритий борг

**`app/handlers/user.py:1031-1035` — текстовий ваучер `VOLTie100` / `VOLT100`.**
Прямий `UPDATE users SET balance = balance + $1` плюс прямий
`INSERT INTO kw_transactions`. Це **четверте** порушення правила «одна точка запису»
(після OCPI CDR, Monobank webhook і Telegram-оплати) — і єдине ще не виправлене.

Три наслідки, кожен окремо неприємний:

1. **Немає ідемпотентності** — код можна ввести необмежену кількість разів
2. **Немає запису в `payments`** → `reconcile_payments.py` цього шляху **не бачить взагалі**
3. **Немає тестів** — `test_voucher_state_menu_bypass.py` перевіряє лише перехоплення
   кнопок меню; рядок `VOLTie100` у тестах не зустрічається жодного разу

Це найдорожчий відкритий пункт у репозиторії. Наступний бандл має починатися з нього.

Разові скрипти `add_deposit.py:8` (INSERT без оновлення балансу, хардкод `user_id=12345`)
і `fix_db.py:8` (`UPDATE ... SET user_id = 514533557`) теж пишуть напряму — але це
офлайн-інструменти, не прод-шлях. Тримати їх у корені все одно небезпечно.

### 6.4. Модель B: hold → finalize / cancel

Клієнт банку — `app/services/monobank_acquiring.py`: `create_invoice()` (`:66`),
`get_invoice_status()` (`:125`), `finalize_invoice()` (`:155`), `cancel_invoice()` (`:197`),
`verify_merchant_token()` (`:225`). `INVOICE_TTL_SECONDS = 900`. Автентифікація —
`X-Token` мерчанта оператора, розшифрований Fernet.

Ланцюг статусів `charging_reservations`:

```
awaiting_hold → pending → active → settling → finalized
                  ↓         ↓         ↓
              cancelled / expired  (звірка)
```

1. `/ocpp_start_uah` → `create_invoice(paymentType=hold)` → **`awaiting_hold`**
2. Вебхук `/webhook/charging-hold/{operator_id}` → перепит банку, вимога статусу рівно
   `hold` → `mark_reservation_hold_confirmed()` (атомарний мьютекс `awaiting_hold→pending`)
   → `_remote_start_with_compensation()`; провал RemoteStart → `cancel_invoice()`
3. `StartTransaction` → **`active`**
4. `StopTransaction` → `claim_reservation_for_settlement()` (`active→settling` —
   **головний захист від подвійного дзвінка в банк**) → `compute_uah_settlement_amount()`
   (чиста функція, `tariff_start + tariff_kwh*kwh`, **капована сумою холду**) →
   сума > 0 → `finalize_invoice()`, сума == 0 → `cancel_invoice()` → **`finalized`**

`MonobankError` на кроці 4 лишає рядок у `settling` і **не** перевикидається в
OCPP-відповідь — дочищає звірка.

**Перевірено на проді 30.07.2026 реальною карткою:** hold→finalize (50→35 грн) і
hold→cancel відпрацювали; банк відхиляє over-capture і повторний finalize (обидва
HTTP 400, errCode 1001). Банк шле **шторм вебхуків** (3 рази за 250 мс) і проміжний
статус `processing` з оманливим `finalAmount` — обидва враховані.

### 6.5. Звірки

| скрипт | що робить |
|---|---|
| `reconcile_payments.py` | `payments` ↔ `kw_transactions`: успіх без нарахування, нарахування без платежу, невідповідність суми (допуск 0,05 кВт·год). `--days`, дефолт 7 |
| `reconcile_operators.py` | застряглі `pending` → перепит банку через той самий `apply_bank_status()`; `success` без доходу → `complete_paid_session()`; алерти. `--stale-minutes`, дефолт 60 |
| `reconcile_charging_reservations.py` | 626 рядків, обидві моделі. kWh: застряглі `pending`/`active` → повний release. UAH: `awaiting_hold` (4 гілки; `hold` і `success` — **лише алерт**, рядок недоторканий, бо RemoteStart з окремого процесу неможливий), `pending` → cancel → `expired`, `settling` → довершити перерваний finalize |

**Планувальника немає.** Ні cron, ні APScheduler, ні systemd-таймера — у репозиторії
відсутні повністю. Звірки запускаються руками:
`docker compose exec bot python reconcile_*.py`. Це відкритий ризик: механізм
самозцілення існує, але ніхто його не викликає.

---

## 7. База даних

**Alembic — джерело правди схеми.** Ланцюг: `b1b193e2bd7b` → `0007` → `0008` → `0010`
→ … → `0018`. Ревізії `0009` не існує.

**Але поруч живе idempotent-бутстрап**, який виконується при кожному старті:
`create_tables()` (`connection.py:64-164`), `init_ocpi_tables()` (`ocpi_repo.py:6-73`),
`init_operator_tables()` (`operators_repo.py:74-346`). **10 таблиць описані в обох місцях.**
При зміні схеми оновлювати обидва, доки дубль існує.

### Реальні розходження, знайдені 06.08.2026

1. **`payment_provider` ENUM не збігається.** Alembic (`b1b193e2bd7b:25`):
   `('liqpay','monobank')`. Бутстрап (`connection.py:79`): `('monobank','telegram')`.
   Обидва створюють тип лише «якщо не існує» → на базі, піднятій Alembic-міграцією,
   значення `'telegram'` **не існує**, і `INSERT ... provider='telegram'` впаде.
   Це прямо б'є по Telegram Payments (`user.py:1085`). **Той самий клас бага, що вже
   стався з `refund` і був закритий міграцією 0008.** Лікується міграцією
   `ALTER TYPE payment_provider ADD VALUE 'telegram'`
2. **`users` і `stations` створює тільки бутстрап** — жодна міграція їх не створює.
   `alembic upgrade head` на порожній базі дає `payments` з FK на неіснуючу `users`
3. **Міграція `0007_ocpi_locations_module` порожня** (`upgrade()` = `pass`), при цьому
   займає слот у ланцюгу. Таблиці `ocpi_*` створює тільки бутстрап
4. `payments.status`: Alembic — `NOT NULL DEFAULT 'pending'`; бутстрап — без `NOT NULL`
5. `payments.user_id`: Alembic — `BIGINT NOT NULL` без FK; бутстрап — з FK на `users`, nullable
6. Набори індексів різні: Alembic робить `idx_payments_user_id`, бутстрап — `idx_payments_invoice`
7. Таблиця `tariffs` створюється **ліниво всередині** `save_ocpi_tariff()`
   (`connection.py:323`) — поза обома механізмами

---

## 8. Зовнішні інтеграції

| інтеграція | модуль | env | мок |
|---|---|---|---|
| Monobank Acquiring | `app/services/monobank_acquiring.py` | `MONOBANK_ACQUIRING_BASE_URL`; токен — **не з env**, а з `operators.monobank_token_encrypted` (Fernet) | `mock_monobank.py` (738 рядків) |
| Open Charge Map | `app/services/ocm_service.py` | `OCM_KEY` | немає; кеш `aiocache` TTL 300 с |
| TomTom | `app/services/tomtom_service.py` | `TOMTOM_API_KEY` (порожньо → тихо вимкнено) | немає; бюджет `TOMTOM_DAILY_BUDGET=2000` |
| Telegram Bot API | `app/core/loader.py` | `BOT_TOKEN`, `LOGS_CHAT_ID` | немає |
| Telegram Payments | `user.py:877/1043/1047` | `PAYMENT_PROVIDER_TOKEN`, `TELEGRAM_PAYMENTS_ENABLED` (дефолт вимкнено) | немає |
| Google Gemini | `loader.py:117`, використання `user.py:1165/1216` | `GEMINI_API_KEY` | немає |
| OCPI 2.2.1 | `app/api/ocpi.py`, `app/services/ocpi/` | `OCPI_SECRET_TOKEN`, `OCPI_CPO_BASE_URL`, `EMSP_BASE_URL` | `mock_cpo.py` |
| **ПРРО / Checkbox** | **коду немає** | — | — |

Ключ TomTom іде в query-рядку, тому логер `httpx` примусово знижений до WARNING
(`app/main.py:44`), щоб URL із ключем не потрапив у логи.

---

## 9. Конфігурація

Повний перелік — `.env.example` (83 рядки). **Валять застосунок при відсутності:**

1. **`OCPI_SECRET_TOKEN`** — `RuntimeError` на імпорті (`app/services/ocpi/config.py:14`).
   Навмисно, дефолту немає. Валить **увесь** застосунок, бо `main.py:21` імпортує `app.api.ocpi`
2. **`BOT_TOKEN`** — `TokenValidationError` у конструкторі `Bot()` (`loader.py:92`)
3. **`GEMINI_API_KEY`** — `genai.Client(api_key=None)` (`loader.py:117`)
4. `DB_URL` — не валить імпорт, але `init_postgres()` після 5 спроб перевикидає виняток

**Тиха деградація:** `ENCRYPTION_KEY` (лише WARNING — але білінг операторів
непрацездатний), `TOMTOM_API_KEY`, `OCM_KEY`, `WALLET_OPERATOR_ID`, `BOT_USERNAME`, `REDIS_HOST`.

Читаються з коду, але **відсутні в `.env.example`**: `REDIS_HOST`, `REDIS_PORT`,
`ANTHROPIC_API_KEY`.

Перед `cat >> .env` перевіряти `tail -c1 .env | xxd` — файл має закінчуватись `\n`.

---

## 10. Тести

**41 файл `test_*.py`** у корені (не в пакеті), ~730 тест-функцій.
`conftest.py` виставляє `OCPI_SECRET_TOKEN` до імпортів. `pytest.ini`: `asyncio_mode=auto`,
`--ignore` для `test_db.py`, `test_ocpi_client.py`, `test_ocpi_commands.py` (старі
`print`-скрипти без assert-ів).

CI (`.github/workflows/ci.yml`): `secret-scan` → `test` → `live-db-tests`
(Postgres 16 + два live-тести).

**Покрито добре:** резервації й обидві моделі розрахунку, OCPP Central System,
кабінет оператора, tenant-ізоляція, QR-флоу, wallet-поповнення, всі три звірки, check_secrets.

**Не покрито:** текстовий ваучер (§6.3), `app/services/ocpi/locations.py`,
`app/handlers/charge.py`, разові скрипти.

---

## 11. OCPI — законсервовано

Звірено 06.08.2026 скриптом `check_ocpi.py`. Детальний розбір — `CLAUDE.md` §6a.

Коротко: код **підключений** до обох точок входу (`main.py:135` і `:154`), таблиці
створюються при кожному старті (`main.py:51`), але клієнтська частина дивиться в мок
(`OCPI_CPO_BASE_URL` дефолт `http://127.0.0.1:8080`), партнера-CPO немає, і останній
коміт по OCPI — **17.07.2026** при живому репозиторії до 03.08.

`POST /ocpi/emsp/2.2.1/cdrs` викликає `update_user_balance()` — це відкритий грошовий
ендпоінт без легітимного споживача, захищений одним статичним токеном.

**Рішення після перевірки проду:** якщо `ocpi_cdrs` порожня і в логах тиша — прибрати
обидва `include_router`, код лишити в репозиторії.

---

## 12. Мертвий і підозрілий код

- **`app/handlers/charge.py`** — роутер підключений (`main.py:158`), але недосяжний
  **двічі**: фільтр `F.text == "Зарядка ⚡️"` містить variation selector `U+FE0F`,
  а реальна кнопка — `"Зарядка ⚡"` без нього (`app/keyboards/reply.py:6`); і навіть за
  збігу його перехопив би `user.py:205`, бо `user_router` реєструється раніше
- **`app/states/charge_states.py`** (`ChargingStates`) — ніхто не імпортує
- **`app/services/ocpi/locations.py`** — 200+ рядків із власним `APIRouter` і `PUT`,
  ніде не імпортується. Згенерований `ai_agent/` — тим самим CrewAI, що лежить у репо
- **`migrations/versions/0007`** — `pass` в `upgrade()` і `downgrade()`
- **`save_ocpi_tariff()` існує двічі** з різною сигнатурою і різними таблицями:
  `connection.py:319` (таблиця `tariffs`) і `ocpi_repo.py:135` (таблиця `ocpi_tariffs`)
- **`PUBLIC_BASE_URL`** з однаковим фолбек-ланцюгом продубльований у 4 місцях
- **`global_error_handler`** (`main.py:163`) повертає `True` на **будь-яку** помилку —
  глушить усе після логування
- **`get_db_pool()`** (`connection.py:18-40`) — busy-wait `for _ in range(50):
  await asyncio.sleep(0.1)` замість `asyncio.Lock`: приховане 5-секундне очікування
  при конкурентному старті
- **`docker-compose.yml:28`** монтує `.:/app` — у проді код у контейнері перекривається
  хостовою текою разом із `.env`
- **18 разових скриптів у корені**, з них із небезпечними хардкодами: `add_deposit.py`,
  `fix_db.py`, `check_db.py` (усі про `user_id=12345`)

`TODO`/`FIXME`/`HACK` у `app/` — **жодного**. Замість них великі пояснювальні
коментарі: це стиль репозиторію, зберігати його.

---

## 13. `ai_agent/`

CrewAI + `langchain_anthropic`, два агенти проєктують і генерують OCPI Locations —
тобто саме той код, що лежить непідключеним у `app/services/ocpi/locations.py`.
З прод-коду **не імпортується**; `crewai` немає в `requirements.txt`, він у
`requirements-dev.txt`.

**Рішення (`CLAUDE.md` §5): лишається на майбутнє, не видаляти.** Але `Dockerfile:5`
робить `COPY . .` — тека потрапляє в образ мертвим вантажем. Споріднений
`refactor_crew.py` у корені — те саме.

---

## 14. Борг за пріоритетом

1. **Ваучер `VOLTie100` пише в обхід єдиної точки, без ідемпотентності й без тестів** (§6.3)
2. **`payment_provider` ENUM розходиться між Alembic і бутстрапом** — Telegram Payments
   впаде на базі, піднятій міграціями (§7.1)
3. **Звірки не мають планувальника** — механізм самозцілення є, викликати нікому (§6.5)
4. **OCPI: відкритий грошовий ендпоінт без споживача** — закривається після перевірки проду (§11)
5. `users`/`stations` не створюються Alembic — чиста база з міграцій непрацездатна (§7.2)
6. Разові скрипти з хардкодами `user_id` у корені (§12)

---

## 15. Що вирішено — не переглядати без причини

- **Гроші напряму на мерчант оператора.** Платформа коштів не торкається: немає
  платіжного посередництва, немає регулювання НБУ
- **Кабінет оператора — тільки Telegram.** Вебчастини немає й не планується
- **`ai_agent/` лишається**, у прод-образ не тягнути
- **Alembic — джерело правди**, бутстрап — тимчасовий дубль
- **Модель B (hold → finalize / cancel)** — водій платить за спожите
- **Напрям накопичувачів — в окремому репозиторії `evolt-bess`**, документи `docs/BESS-*.md`
- **Бізнес-стратегія — в `evolt-business`**, не в кодових репозиторіях
