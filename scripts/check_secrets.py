#!/usr/bin/env python3
"""
Бар'єр проти секретів у комітах.

Причина: два витоки ключів 31.07.2026 (TomTom-ключ у логах httpx,
Anthropic-ключ, зашитий в auth_check.py з 2026-07-16) — обидва знайдено
випадково, жоден не спіймано процесом. Скрипт перевіряє ЛИШЕ ДОДАНІ рядки
(рядки, що зʼявляються в диффі як "+") на форми, типові для секретів:
відомі префікси провайдерів, присвоєння виду ІМ'Я=значення для чутливих
імен, спробу закомітити сам файл .env.

Хибне спрацювання тут дорожче за пропуск: бар'єр, що кричить на кожен
коміт, вимкнуть за тиждень і стане гіршим за відсутній. Тому — явний
allow-list шляхів (тести, моки) і плейсхолдерних значень нижче. Шлях
потрапляє в ALLOWLISTED_PATH_PATTERNS лише після ЕМПІРИЧНОЇ перевірки —
прогнати сканер по реальному вмісту й показати 0 хибних спрацювань; якщо
вони є, звужуй правило (як БЛОКЕР 1 round 2), а не ховай шлях цілком.
Розширюйте САМЕ ЦІ константи (ALLOWLISTED_PATH_PATTERNS, PLACEHOLDER_VALUES),
коли зʼявиться новий легітимний хибний збіг — не регулярки вище них.

Використання:
    python scripts/check_secrets.py                     # git diff --cached
    python scripts/check_secrets.py --base origin/main   # диф проти ref (CI, PR)
    python scripts/check_secrets.py file1.py file2.py    # повний вміст файлів

Код виходу: 0 — чисто; 1 — знайдено підозріле.

У вивід НІКОЛИ не друкується знайдене значення цілком — лише перші кілька
символів (див. _truncate_snippet) — інакше сам скрипт стає ще одним місцем,
де секрет лишається в логах CI.
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple


# --- Відомі префікси провайдерів --------------------------------------------
# Кожен перевіряється незалежно; порядок значення не має.
SECRET_PREFIXES: Tuple[str, ...] = (
    "sk-ant-",
    "sk-",
    "ghp_",
    "gho_",
    "ghs_",
    "github_pat_",
    "AIza",
    "xoxb-",
    "xoxa-",
    "xoxp-",
    "xoxr-",
    "xoxs-",
    "glpat-",
    "AKIA",
)

PRIVATE_KEY_HEADER_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")

# Позиційні секрети: форма, що видає себе незалежно від того, чи стоїть вона
# праворуч від "NAME=" (round 2 рев'ю, не-блокер 3 — `bot = Bot("885...")`,
# `f"…?key=aBcD…"` жодною попередньою перевіркою не ловились).
#
# Telegram bot-токен: `<id 8-10 цифр>:AA<30+ символів>`. Форма настільки
# специфічна (сам Telegram генерує рівно так), що хибне спрацювання
# практично неможливе — це найдорожчий секрет цього репозиторію (повний
# контроль над ботом).
TELEGRAM_TOKEN_RE = re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_\-]{30,}\b")

# Секрет у query-рядку URL — рівно та форма, якою TomTom-ключ уже один раз
# витік у логи httpx 31.07.2026 (?key=...).
URL_SECRET_QUERY_RE = re.compile(
    r"[?&](?:key|api_key|apikey|token|secret|password)=[A-Za-z0-9_\-]{20,}",
    re.IGNORECASE,
)

# Рядок підключення з паролем усередині (round 3 рев'ю, не-блокер Н2):
# `postgresql://evolt:change_me@db:5432/ev` — незалежно від того, під яким
# іменем змінної стоїть (DB_URL не містить KEY/TOKEN/SECRET/PASSWORD, тому
# "чутливе присвоєння" на нього взагалі не дивиться). Схема, юзер, 6+
# символів пароля, "@" — така форма не буває випадковою.
#
# "$", "{", "}" виключені з обох частин навмисно: `docker-compose.yml` цього
# репозиторію (реальний трекнутий файл, рядок 26) пише
# `postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres/...`
# — без цього винятку сам приклад із рев'ю ловив би шаблонну підстановку,
# а не значення (перевірено запуском ДО додавання винятку — спіймало).
#
# Іменована група "password" — щоб те саме `is_placeholder_value()`, що вже
# фільтрує "NAME=значення", можна було застосувати й тут. Без цього
# позиційна перевірка ловила б `.env.example` (`DB_URL=...:CHANGE_ME@...`,
# реальний трекнутий рядок) і власний CI-фікстур `ci_test_password`
# (`.github/workflows/ci.yml`) — спіймано повним прогоном по 166
# трекнутих файлах ПЕРЕД тим, як додати цей виняток.
CONNECTION_STRING_RE = re.compile(
    r"[a-z][a-z0-9+.\-]*://[^\s:@/${}]+:(?P<password>[^\s@/${}]{6,})@"
)


def _has_real_connection_string_password(text: str) -> bool:
    m = CONNECTION_STRING_RE.search(text)
    return bool(m) and not is_placeholder_value(m.group("password"))

# Скільки символів має йти ОДРАЗУ ЗА префіксом, щоб рахувати це реальним
# токеном, а не словом, що просто так починається з тих самих букв
# ("sky...", "glpatience...").
MIN_TOKEN_TAIL_LENGTH = 15

_TOKEN_CHARS_RE = re.compile(r"[A-Za-z0-9_\-]+")


# --- ІМ'Я=значення для чутливих імен -----------------------------------------
# Необов'язковий "- " на початку — YAML-форма списку змінних середовища
# (round 3 рев'ю, не-блокер Н1): `docker-compose.yml` цього репозиторію пише
# `environment:` саме так (`      - POSTGRES_PASSWORD=...`), а не `NAME:
# value`. Без цього "- " рядок не матчився взагалі.
ENV_ASSIGNMENT_RE = re.compile(
    r"^\s*(?:-\s+)?(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*$"
)
SENSITIVE_NAME_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD)", re.IGNORECASE)
MIN_SENSITIVE_VALUE_LENGTH = 20

# Значення має бути ЛІТЕРАЛОМ секретної форми, а не довільним python-виразом
# (round 2 рев'ю: `token_encrypted = await repo.get_operator_token(...)`,
# `api_key = os.getenv("GEMINI_API_KEY")`, `key_builder=location_key_builder,`
# і подібні 48 хибних спрацювань на 80 production-файлах — усе це вирази, не
# літерали). Пробіли/дужки/крапки з комою тут ніколи не бувають у справжньому
# секреті і завжди є у виклику функції чи await-виразі.
_SECRET_SHAPE_RE = re.compile(rf"^[A-Za-z0-9_\-./+=:]{{{MIN_SENSITIVE_VALUE_LENGTH},}}$")

# Якщо значення суцільно з [A-Za-z0-9_] (жодного з "-./+=:" — саме такий
# шматок радше python-ідентифікатор, ніж секрет), додатково вимагаємо доказ,
# що це не просто слово: або "цифра І різний регістр" (ловить TomTom-ключ
# `aBcD3fGh1jKlMnOpQrStUvWxYz012345`, не ловить `location_key_builder`), або
# суцільний hex довжиною 32+ (ловить lowercase-hex ключі на кшталт OCM_KEY
# цього проєкту; поріг саме 32, не менше — щоб НЕ чіпати Alembic revision id,
# які теж hex, але 12 символів, напр. `b1b193e2bd7b`).
_PURE_ALNUM_UNDERSCORE_RE = re.compile(r"^[A-Za-z0-9_]+$")
_HEX_LOWER_RE = re.compile(r"^[0-9a-f]{32,}$")
_HEX_UPPER_RE = re.compile(r"^[0-9A-F]{32,}$")

# round 3 рев'ю, не-блокер Н3: URL-константа під "чутливим" іменем
# (`MONOBANK_TOKEN_URL = "https://api.monobank.ua/..."`) формально проходила
# _SECRET_SHAPE_RE (лише дозволені символи, довжина ≥20) — ендпоінт, не
# секрет. Відкидаємо значення, що починаються зі схеми URL, КРІМ випадку,
# коли всередині є user:pass@ (Н2 — це вже реальний рядок підключення).
_URL_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.IGNORECASE)


# --- Allow-list: РОЗШИРЮВАТИ ЦІ СПИСКИ, а не регулярки вище -----------------

# Значення, що формою виглядають як секрет (довгі, під "чутливим" імʼям), але
# завідомо ні — типові плейсхолдери в .env.example/тестах/докстрінгах.
# Порівняння регістронезалежне, лапки/пробіли обрізаються (див.
# _normalize_placeholder). Додавайте сюди новий плейсхолдер, коли він
# реально зʼявиться в коді — не розширюйте це вгадуванням наперед.
PLACEHOLDER_VALUES = {
    "your_key",
    "your-key",
    "your_key_here",
    "your-key-here",
    "твій_ключ",
    "твій_старий_ключ",
    "change_me",
    "change-me",
    "changeme",
    "fake-key",
    "fake_key",
    "encrypted-token",
    "encrypted_token",
    "test-token",
    "test_token",
    "placeholder",
    "dummy-secret",
    "dummy_secret",
    # round 3: реальний CI-фікстур цього репозиторію (.github/workflows/ci.yml,
    # POSTGRES_PASSWORD/DB_URL для job live-db-tests) — не секрет, пароль
    # тестового Postgres-контейнера, що піднімається й одразу знищується в CI.
    "ci_test_password",
}

# Шляхи (fnmatch-патерни; звіряються і з повним POSIX-шляхом файлу відносно
# кореня репо, і окремо з самою назвою файлу), де секрето-подібні рядки —
# очікуваний контент, а не витік: тести (справжній контент за призначенням —
# фейкові токени в самому test_check_secrets.py включно) і моки з вигаданими
# значеннями за формою реальних.
#
# round 2 рев'ю: `.env.example` і `docs/*.md` тут БУЛИ, поки не звузили
# правило "чутливе присвоєння" (БЛОКЕР 1). Емпіричний тест ПІСЛЯ фіксу
# (scan_line на реальному вмісті всіх docs/*.md і .env.example) дав 0
# спрацювань на обох — тобто повне виключення шляху ховало б реальний ключ,
# якщо його колись туди вставлять, БЕЗКОШТОВНО (жодного хибного
# спрацювання не втрачаємо, прибираючи їх звідси). Прибрано з allow-list;
# перевірено тестом test_no_false_positives_on_real_docs_and_env_example
# (лишається зеленим — контент і зараз чистий) і
# test_real_secret_in_docs_and_env_example_now_caught (дірка закрита).
ALLOWLISTED_PATH_PATTERNS: Tuple[str, ...] = (
    "test_*.py",
    "mock_monobank.py",
    "mock_cpo.py",
    "README.md",
)

# round 2 рев'ю, не-блокер 6: `.env` точним рядком пропускав `.env.prod`,
# `.env.local` тощо — той самий клас файлу, та сама заборона в .gitignore
# (тепер `.env*` там теж). `.env.example` — єдиний свідомий виняток
# (навмисно комітиться, значення завжди порожні).
ENV_FILENAME_PATTERN = ".env*"
ENV_EXAMPLE_FILENAME = ".env.example"


@dataclass(frozen=True)
class Finding:
    file: str
    line_number: int  # 0, якщо знахідка стосується самого файлу, не рядка
    reason: str
    snippet: str


def _truncate_snippet(value: str, keep: int = 4) -> str:
    value = value.strip()
    if not value:
        return ""
    return value[:keep] + "..."


def _normalize_placeholder(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v.strip().lower()


def is_placeholder_value(value: str) -> bool:
    return _normalize_placeholder(value) in PLACEHOLDER_VALUES


def _strip_wrapping(value: str) -> str:
    """Обрізати кінцеву кому/пробіли й парні зовнішні лапки, лишивши сам літерал.

    "location_key_builder," (kwarg-рядок) -> "location_key_builder";
    '"sk-ant-…"' -> "sk-ant-…". Порядок важливий: кома може стояти ПІСЛЯ
    закриваючої лапки (typowий kwarg), тому обрізаємо її ДО зняття лапок.
    """
    v = value.strip()
    if v.endswith(","):
        v = v[:-1].rstrip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v


def _is_secret_shaped_literal(value: str) -> bool:
    v = _strip_wrapping(value)
    if not _SECRET_SHAPE_RE.fullmatch(v):
        return False
    if _URL_SCHEME_RE.match(v):
        # URL сам по собі — не секрет (ендпоінт), КРІМ рядка підключення з
        # НЕплейсхолдерним паролем усередині (та сама форма, що Н2).
        return _has_real_connection_string_password(v)
    if _PURE_ALNUM_UNDERSCORE_RE.fullmatch(v):
        has_digit = any(c.isdigit() for c in v)
        has_mixed_case = any(c.islower() for c in v) and any(c.isupper() for c in v)
        if has_digit and has_mixed_case:
            return True
        return bool(_HEX_LOWER_RE.fullmatch(v) or _HEX_UPPER_RE.fullmatch(v))
    # Містить хоч один із "-./+=:" — за формою вже не голий python-ідентифікатор.
    return True


def _to_posix(path: str) -> str:
    return path.replace("\\", "/")


def is_allowlisted_path(path: str) -> bool:
    posix_path = _to_posix(path)
    name = posix_path.rsplit("/", 1)[-1]
    for pattern in ALLOWLISTED_PATH_PATTERNS:
        if fnmatch.fnmatch(posix_path, pattern) or fnmatch.fnmatch(name, pattern):
            return True
    return False


def is_env_file(path: str) -> bool:
    posix_path = _to_posix(path)
    name = posix_path.rsplit("/", 1)[-1]
    if name == ENV_EXAMPLE_FILENAME:
        return False
    return fnmatch.fnmatch(name, ENV_FILENAME_PATTERN)


def _extract_token(line: str, start: int) -> str:
    m = _TOKEN_CHARS_RE.match(line, start)
    return m.group(0) if m else ""


def _scan_prefixes(line: str) -> List[Tuple[str, str]]:
    hits: List[Tuple[str, str]] = []
    for prefix in SECRET_PREFIXES:
        search_from = 0
        while True:
            idx = line.find(prefix, search_from)
            if idx == -1:
                break
            token = _extract_token(line, idx)
            if len(token) - len(prefix) >= MIN_TOKEN_TAIL_LENGTH:
                hits.append((f"відомий префікс секрету ({prefix})", token))
            search_from = idx + 1
    return hits


def scan_line(line: str) -> List[Tuple[str, str]]:
    """Чиста функція: рядок -> [(причина, знайдений фрагмент), ...].

    Без жодного знання про файл/шлях — allow-list за шляхом застосовується
    на рівень вище, у scan_added_lines().
    """
    hits: List[Tuple[str, str]] = _scan_prefixes(line)

    if PRIVATE_KEY_HEADER_RE.search(line):
        hits.append(("заголовок приватного ключа", "-----BEGIN"))

    m = ENV_ASSIGNMENT_RE.match(line)
    if m:
        name, value = m.group(1), m.group(2)
        if (
            SENSITIVE_NAME_RE.search(name)
            and not is_placeholder_value(value)
            and _is_secret_shaped_literal(value)
        ):
            hits.append((f"чутливе присвоєння ({name})", value))

    tg_match = TELEGRAM_TOKEN_RE.search(line)
    if tg_match:
        hits.append(("Telegram bot-токен у рядку", tg_match.group(0)))

    url_match = URL_SECRET_QUERY_RE.search(line)
    if url_match:
        hits.append(("секрет у query-рядку URL", url_match.group(0)))

    conn_match = CONNECTION_STRING_RE.search(line)
    if conn_match and not is_placeholder_value(conn_match.group("password")):
        hits.append(("рядок підключення з паролем", conn_match.group(0)))

    return hits


def _dedupe_findings(findings: Sequence[Finding]) -> List[Finding]:
    """Один (файл, рядок) -> один Finding, навіть якщо спрацювало кілька правил.

    round 2 рев'ю, не-блокер 4: `key = "sk-ant-…"` раніше давав 3 окремих
    Finding (два префікси-підмножини + чутливе присвоєння) на ОДИН реальний
    секрет — підсумкове число в консолі вводило в оману. Порядок збігів у
    межах об'єднаного рядка зберігається (перше правило, що спрацювало,
    першим у reason); snippet бере перше НЕпорожнє значення.
    """
    merged: Dict[Tuple[str, int], Finding] = {}
    order: List[Tuple[str, int]] = []
    for f in findings:
        key = (f.file, f.line_number)
        if key not in merged:
            merged[key] = f
            order.append(key)
            continue
        existing = merged[key]
        reasons = existing.reason.split("; ")
        if f.reason not in reasons:
            reasons.append(f.reason)
        snippet = existing.snippet or f.snippet
        merged[key] = Finding(existing.file, existing.line_number, "; ".join(reasons), snippet)
    return [merged[key] for key in order]


def scan_added_lines(
    path: str, numbered_lines: Sequence[Tuple[int, str]]
) -> List[Finding]:
    """path + [(номер_рядка, вміст), ...] доданих рядків -> список Finding.

    Дедуплікує знахідки за (файл, номер_рядка) — один реальний секрет, що
    підпадає під кілька правил (напр. і префікс, і "чутливе присвоєння"),
    дає РІВНО один Finding з об'єднаною причиною, а не N окремих.
    """
    findings: List[Finding] = []

    if is_env_file(path):
        findings.append(Finding(path, 0, "спроба додати файл .env", ""))

    if is_allowlisted_path(path):
        return findings

    for line_number, line in numbered_lines:
        for reason, raw in scan_line(line):
            findings.append(Finding(path, line_number, reason, _truncate_snippet(raw)))

    return _dedupe_findings(findings)


def parse_diff_added_lines(diff_text: str) -> Dict[str, List[Tuple[int, str]]]:
    """Unified diff -> {шлях_файлу: [(номер_рядка, вміст), ...]} ЛИШЕ додані рядки.

    Чиста функція над текстом — не потребує реального git-репозиторію,
    тому легко тестується на синтетичних диффах.
    """
    files: Dict[str, List[Tuple[int, str]]] = {}
    current_path: str | None = None
    new_lineno: int | None = None

    for raw in diff_text.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:]
            if target == "/dev/null":
                current_path = None
            else:
                current_path = target[2:] if target.startswith("b/") else target
                files.setdefault(current_path, [])
            new_lineno = None
            continue

        if raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            new_lineno = int(m.group(1)) if m else 1
            continue

        if current_path is None or new_lineno is None:
            continue

        if raw.startswith("+"):
            files[current_path].append((new_lineno, raw[1:]))
            new_lineno += 1
        elif raw.startswith("-"):
            pass  # рядок вилучено — не зʼявляється в новому файлі
        elif raw.startswith("\\"):
            pass  # "\ No newline at end of file"
        else:
            new_lineno += 1  # контекстний рядок (diff з ненульовим --unified)

    return files


def scan_diff_text(diff_text: str) -> List[Finding]:
    findings: List[Finding] = []
    for path, lines in parse_diff_added_lines(diff_text).items():
        findings.extend(scan_added_lines(path, lines))
    return findings


def scan_files_full_content(paths: Sequence[str]) -> List[Finding]:
    """Скан повного вмісту явно переданих файлів (не диффу).

    Порожній `paths` — валідний, штатний вхід (нема staged-файлів), просто
    повертає порожній список.
    """
    findings: List[Finding] = []
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                raw_lines = fh.readlines()
        except OSError:
            continue
        numbered = list(enumerate((l.rstrip("\n") for l in raw_lines), start=1))
        findings.extend(scan_added_lines(path, numbered))
    return findings


class GitDiffError(Exception):
    """`git diff` завершився ненульовим кодом.

    round 3 рев'ю, БЛОКЕР: раніше `_git_diff_against`/`_git_diff_cached`
    ігнорували returncode і повертали result.stdout як є (порожній рядок
    при збої) — "git впав" і "диф порожній" були нерозрізненні,
    scan_diff_text("") давав 0 знахідок, exit 0. Найгірший випадок: `--base`
    на недосяжний ref (типово після force-push) — git exit 128, сканер
    мовчки казав "чисто". Тепер це виняток, а не порожній рядок.
    """


def _run_git_diff(diff_args: Sequence[str]) -> str:
    # encoding="utf-8" явно — без нього subprocess декодує stdout локальною
    # кодовою сторінкою ОС (на Windows типово cp1251/cp1252, не UTF-8), і
    # скрипт падає з UnicodeDecodeError на першому ж українському коментарі
    # в диффі. git завжди пише UTF-8 незалежно від ОС.
    result = subprocess.run(
        ["git", "diff", *diff_args, "--unified=0", "--no-color"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        # Не друкувати result.stderr повністю — там може бути що завгодно
        # (шлях до файлу, вміст якого не хочемо в логах CI); лише сам факт
        # і команда, якою можна відтворити збій вручну.
        raise GitDiffError(
            f"git diff {' '.join(diff_args)} завершився кодом {result.returncode}"
        )
    return result.stdout


def _git_diff_cached() -> str:
    return _run_git_diff(["--cached"])


def _git_diff_against(base_ref: str) -> str:
    return _run_git_diff([f"{base_ref}...HEAD"])


def _format_finding(f: Finding) -> str:
    location = f"{f.file}:{f.line_number}" if f.line_number else f.file
    suffix = f" ({f.snippet})" if f.snippet else ""
    return f"[SECRET?] {location} — {f.reason}{suffix}"


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Бар'єр проти секретів у доданих рядках коду."
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="перевірити повний вміст цих файлів замість git diff",
    )
    parser.add_argument(
        "--base",
        help="диф проти цього ref замість --cached (для CI на PR)",
    )
    args = parser.parse_args(argv)

    if args.paths:
        findings = scan_files_full_content(args.paths)
    else:
        try:
            diff_text = _git_diff_against(args.base) if args.base else _git_diff_cached()
        except GitDiffError as exc:
            print(f"[ERROR] {exc} — вважаємо перевірку НЕ пройденою, не 'чисто'.", file=sys.stderr)
            return 1
        findings = scan_diff_text(diff_text)

    if not findings:
        return 0

    for f in findings:
        print(_format_finding(f), file=sys.stderr)

    word = "знахідка" if len(findings) == 1 else "знахідки(ок)"
    print(
        f"\n{len(findings)} підозріл{'а' if len(findings) == 1 else 'их'} {word} "
        "у доданих рядках. Якщо це реальний секрет — прибери його з диффу й "
        "ротуй ключ. Якщо це хибне спрацювання — розшир allow-list "
        "(PLACEHOLDER_VALUES / ALLOWLISTED_PATH_PATTERNS) у "
        "scripts/check_secrets.py, а не обходь перевірку.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
