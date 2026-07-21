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
                "50 тестів ізоляції тенантів на фейкових з'єднаннях, без живої БД.",
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

## Місця, що потребують ручного рев'ю

- **`record_session_income()`** — дохід і комісія двома рядками в одній
  транзакції, комісія від'ємна. Формула `round(amount * pct / 100, 2)`:
  напрямок округлення (на користь оператора чи платформи) не обговорювався.
- **Кешованого балансу оператора немає** — `get_operator_balance()` це
  `SUM(amount_uah)` по журналу. Свідомо, щоб не повторити розсинхрон
  `users.balance` <-> `kw_transactions`.
- **`monobank_token_encrypted`** — репозиторій бачить лише зашифрований
  токен, шифрування (Fernet на `ENCRYPTION_KEY`) робить шар вище (Промпт 2).
  Виключений з усіх звичайних `SELECT`, дістається окремою функцією.

## Прогін тестів

```
pytest test_operator_isolation.py -q                  -> 50 passed
pytest -q (з плейсхолдерами CI env)                   -> 103 passed
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
