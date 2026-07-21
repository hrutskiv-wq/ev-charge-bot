#!/usr/bin/env python3
"""
Збирає review_prompt<N>.md — один файл з повним кодом усього, що зробив
черговий промпт White-Label білінгу, для рев'ю однією стрічкою.

Навіщо окремий скрипт, а не ручна збірка: код у review-файлі читається
ПРЯМО з робочої копії, тому він фізично не може розійтися з тим, що лежить
у комітах. Ручне копіювання в markdown такої гарантії не дає — і саме через
розходження двох копій однієї сутності цей проєкт уже ловив баги
(див. PROJECT_CONTEXT.md, п.8 про 'refund').

Запуск (з кореня репозиторію або будь-звідки — шлях визначається сам):
    python scripts/gen_review.py 1

Результат: review_prompt1.md у корені репозиторію. Ці файли в .gitignore
(правило review_prompt*.md) — вони дублюють коміти й швидко застарівають,
тому перегенеровуються за потреби, а не зберігаються в історії.

Щоб додати наступний промпт — допишіть запис у PROMPTS нижче. Опис до
кожного файлу пишеться руками навмисно: він пояснює РОЛЬ файлу, чого з
самого коду не видно.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PROMPTS = {
    "1": {
        "title": "Промпт 1 — Тенанти і станції",
        "intro": (
            "White-Label білінг, Промпт 1 з `docs/evolt-white-label-bilinh-ta-p2p.md`:\n"
            "оператори зарядних станцій як тенанти, їхні станції, сесії зарядки та\n"
            "журнал розрахунків."
        ),
        "files": [
            (
                "migrations/versions/0010_white_label_tenants.py",
                "НОВИЙ",
                "Alembic-міграція 0010 — джерело правди для схеми. "
                "`down_revision='0008_add_refund_type'`.",
            ),
            (
                "app/database/operators_repo.py",
                "НОВИЙ",
                "Репозиторій мультитенантного білінгу + idempotent-дзеркало схеми "
                "(`init_operator_tables()`).",
            ),
            (
                "test_operator_isolation.py",
                "НОВИЙ",
                "60 тестів ізоляції тенантів, ідемпотентності нарахувань і "
                "округлення комісії — на фейкових з'єднаннях, без живої БД.",
            ),
            (
                "app/main.py",
                "ЗМІНЕНО",
                "Підключення `init_operator_tables()` у FastAPI lifespan (+2 рядки).",
            ),
        ],
        # Файли, де зміна маленька відносно розміру файлу: показуємо diff, а
        # повний код ховаємо під <details>, щоб реальна зміна не потонула.
        "diff_only": ["app/main.py"],
        "sections": """## Ланцюг міграцій

Перевірено `alembic history` — голова одна, розгалуження немає:

```
<base> -> b1b193e2bd7b -> 0007_ocpi_locations_module -> 0008_add_refund_type -> 0010_white_label_tenants (head)
```

Файл названо `0010` відповідно до стратегічного документа, але в ланцюгу
ревізія йде одразу після `0008` — міграції `0009` у репозиторії немає.

## Три механізми ізоляції тенантів

1. **Репозиторій** — `operator_id` перший аргумент кожної тенант-скоупнутої
   функції й обов'язково у `WHERE`. Параметризований тест обходить усі 17
   функцій; нова функція без фільтра впаде в тестах.
2. **`create_session`** не довіряє аргументу: `operator_id` береться з
   підзапиту по станції (`INSERT ... SELECT ... WHERE s.operator_id = $1`),
   тому для чужої станції сесія просто не створюється.
3. **Композитний FK** `(station_id, operator_id) -> operator_stations(id,
   operator_id)` — те саме обмеження вже на рівні БД.

## Ідемпотентність нарахувань (за результатом рев'ю)

Повторний webhook Monobank, ретрай після таймауту чи подвійне натискання
оператором не мають нарахувати дохід двічі — журнал незмінний, тож зайве
нарахування довелося б гасити ручним рядком `adjustment`.

- Частковий унікальний індекс `uq_ledger_session_income` на
  `(session_id, type)` — одна сесія дає рівно один дохід і одну комісію.
  Рядки без `session_id` (`payout`, `subscription_fee`, `adjustment`) під
  обмеження не підпадають.
- `uq_sessions_payment` — один інвойс Monobank не може бути привʼязаний до
  двох сесій.
- `add_ledger_entry()` пише з `ON CONFLICT DO NOTHING`;
  `record_session_income()` при конфлікті повертає id уже існуючих рядків
  і логує `duplicate income ignored`.

## Місця, що потребують ручного рев'ю

- **`record_session_income()`** — дохід і комісія двома рядками в одній
  транзакції, комісія від'ємна. Рахується в `Decimal` з `ROUND_HALF_UP`
  (не `round()` по float — той округлює до парного і зрізає копійку).
- **Кешованого балансу оператора немає** — `get_operator_balance()` це
  `SUM(amount_uah)` по журналу. Свідомо, щоб не повторити розсинхрон
  `users.balance` <-> `kw_transactions`.
- **`monobank_token_encrypted`** — репозиторій бачить лише зашифрований
  токен, шифрування (Fernet на `ENCRYPTION_KEY`) робить шар вище (Промпт 2).
  Виключений з усіх звичайних `SELECT`, дістається окремою функцією.

## Прогін тестів

```
pytest test_operator_isolation.py -q                  -> 60 passed
pytest -q (з плейсхолдерами CI env)                   -> 113 passed
```

Локально прогін був на Python 3.14 (єдиний у системі); цільова версія
проєкту — 3.11, її перевіряє CI.
""",
    },
    "2a": {
        "title": "Промпт 2a — Еквайринг оператора",
        "intro": (
            "Грошова частина QR-флоу з `docs/evolt-white-label-bilinh-ta-p2p.md`:\n"
            "шифрування еквайринг-токенів операторів, клієнт Monobank Acquiring,\n"
            "платежі водіїв і webhook підтвердження оплати. Сторінка `/s/{qr_slug}`,\n"
            "чек і пуш оператору — Промпт 2b."
        ),
        "files": [
            (
                "app/core/crypto.py",
                "НОВИЙ",
                "Fernet на `ENCRYPTION_KEY`: шифрування токенів операторів, ледача "
                "перевірка ключа.",
            ),
            (
                "app/services/monobank_acquiring.py",
                "НОВИЙ",
                "Клієнт Acquiring API: створення інвойсу й перевірка статусу "
                "токеном мерчанта-оператора.",
            ),
            (
                "app/api/operator_webhook.py",
                "НОВИЙ",
                "`POST /webhook/operator/{operator_id}` — не вірить тілу, перепитує банк.",
            ),
            (
                "migrations/versions/0011_operator_payments.py",
                "НОВИЙ",
                "`operator_payments` + перевішування `operator_sessions.payment_id`.",
            ),
            (
                "mock_monobank.py",
                "НОВИЙ",
                "Мок еквайрингу за зразком `mock_cpo.py` — без нього флоу не "
                "протестувати без реального оператора.",
            ),
            (
                "test_operator_payments.py",
                "НОВИЙ",
                "32 тести: модель довіри webhook, ізоляція тенантів, ідемпотентність, крипто.",
            ),
        ],
        "diff_only": [],
        "sections": """## Модель довіри webhook

```
POST /webhook/operator/{operator_id}
  -> invoiceId з тіла
  -> інвойс має існувати в operator_payments САМЕ цього operator_id (з URL)
  -> GET /api/merchant/invoice/status токеном оператора
  -> віримо ЛИШЕ відповіді банку
```

Тіло webhook не використовується ні для статусу, ні для суми. Причина: інвойси
створені токенами різних мерчантів, тож `x-sign` кожного підписаний ключем
свого оператора — глобальна перевірка з `app/api/payments.py` (наш власний
`MONOBANK_API_TOKEN`) для них не працює в принципі. Замість кешу публічних
ключів на кожного оператора тіло webhook зроблено нерелевантним: навіть маючи
URL і знаючи invoiceId, підробити оплату неможливо.

Невідомий invoiceId → тихий 200 без подробиць: відповідь однакова для «не
існує», «чужий оператор» і «все гаразд», щоб ендпоінт не став оракулом для
зондування.

**Перевірено мутацією:** якщо змусити код читати статус із тіла webhook,
падає рівно `test_webhook_body_claiming_success_does_not_credit_if_bank_disagrees`.

## Чому окрема таблиця, а не наявна `payments`

1. Водій **не реєструється** — у нього немає `users.user_id`, а
   `payments.user_id` оголошено `NOT NULL` у початковій схемі. Рядок для
   водійського платежу туди фізично не вставити.
2. `payments` — журнал НАШИХ поповнень балансу, його читає
   `reconcile_payments.py`. Домішування другої платіжної моделі зламало б звірку.
3. Мультитенантність: кожна таблиця білінгу має `operator_id`.

`UNIQUE (operator_id, invoice_id)` — не глобально, інакше оператор А міг би
«зайняти» ідентифікатор оператора Б.

## Захисти від подвійного нарахування

- Уже успішний платіж не чіпається, банк не смикається повторно
- `UPDATE ... WHERE status <> $3` — мʼютекс: рівно один паралельний webhook
  проводить нарахування
- Сума з банку звіряється з виставленою; розбіжність блокує нарахування
- Банк недоступний → 502, щоб Monobank повторив і оплата не загубилась

## Місця, що потребують ручного рев'ю

- **`ENCRYPTION_KEY` без ротації.** Зміна ключа робить усі збережені токени
  нерозшифровуваними. Процедури перешифрування ще немає.
- **Ледача перевірка ключа** — свідомий вибір: застосунок стартує без ключа
  (решта функціоналу від нього не залежить), лише гучний WARNING.
- **Проміжні статуси банку** (`created`/`processing`/`hold`) нічого не
  змінюють — покладаємось на наступний webhook. Якщо він не прийде, платіж
  лишиться `pending` до звірки (Промпт 5).

## Прогін тестів

```
pytest test_operator_payments.py -q                   -> 32 passed
pytest -q (з плейсхолдерами CI env)                   -> 196 passed
```

Локально прогін був на Python 3.14 (єдиний у системі); цільова версія
проєкту — 3.11, її перевіряє CI.
""",
    },
}


def sh(*args):
    """git-виклик у корені репозиторію; порожній рядок, якщо команда впала."""
    result = subprocess.run(
        args, cwd=ROOT, capture_output=True, text=True, encoding="utf-8"
    )
    return (result.stdout or "").strip()


def github_anchor(path: str) -> str:
    """
    Якір для заголовка виду ``## `шлях` ``: GitHub прибирає беклапки, слеші
    й крапки, ЗБЕРІГАЄ підкреслення і переводить у нижній регістр.
    """
    return path.lower().replace("/", "").replace(".", "")


def build(prompt_key: str) -> Path:
    spec = PROMPTS[prompt_key]
    out = ROOT / f"review_prompt{prompt_key}.md"

    commit = sh("git", "log", "-1", "--format=%h %s")
    branch = sh("git", "rev-parse", "--abbrev-ref", "HEAD")
    base = sh("git", "log", "-1", "--format=%h", "origin/main")
    diffstat = "\n".join(
        line.strip()
        for line in sh("git", "show", "--stat", "--format=", "HEAD").splitlines()
    )

    parts = [
        f"# {spec['title']}: повний код для рев'ю\n\n"
        f"**Гілка:** `{branch}` (від `origin/main` @ `{base}`)\n"
        f"**Комміт:** `{commit}`\n\n"
        f"{spec['intro']}\n\n"
        f"```\n{diffstat}\n```\n\n"
        "## Зміст\n\n"
    ]

    for path, status, purpose in spec["files"]:
        parts.append(f"- [`{path}`](#{github_anchor(path)}) — {status}. {purpose}\n")

    parts.append("\n" + spec["sections"] + "\n---\n")

    for path, status, purpose in spec["files"]:
        target = ROOT / path
        if not target.exists():
            raise SystemExit(f"❌ Немає файлу {path} — оновіть PROMPTS у цьому скрипті.")
        content = target.read_text(encoding="utf-8").rstrip("\n")
        lines = len(content.splitlines())
        parts.append(f"\n## `{path}`\n\n**{status}** · {lines} рядків · {purpose}\n\n")

        collapse = path in spec.get("diff_only", [])
        if collapse:
            diff = sh("git", "show", "HEAD", "--", path)
            if "diff --git" in diff:
                diff = diff[diff.index("diff --git"):]
            parts.append(
                f"Зміна мінімальна відносно розміру файлу — сам diff:\n\n"
                f"```diff\n{diff}\n```\n\n"
                "<details>\n<summary>Повний код файлу</summary>\n\n"
            )
        parts.append(f"```python\n{content}\n```\n")
        if collapse:
            parts.append("\n</details>\n")

    out.write_text("".join(parts), encoding="utf-8")
    return out


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in PROMPTS:
        available = ", ".join(sorted(PROMPTS))
        print(f"Використання: python scripts/gen_review.py <номер промпту>")
        print(f"Доступні: {available}")
        raise SystemExit(1)

    out = build(sys.argv[1])
    line_count = len(out.read_text(encoding="utf-8").splitlines())
    print(f"OK -> {out} ({line_count} рядків)")


if __name__ == "__main__":
    main()
