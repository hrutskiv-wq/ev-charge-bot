"""
Тести бар'єра проти секретів (scripts/check_secrets.py).

Головна вимога до якості цього скрипта — не хибне спрацювання: перевіряємо
не лише "ловить реальні секрети", а й окремо "не гавкає" на фейкові токени
в тестах/моках. `.env.example` і `docs/*.md` (round 2) НЕ в allow-list —
емпірично перевірено, що реальний вміст обох чистий (0 спрацювань), тож
ховати шлях цілком там не потрібно.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.check_secrets import (
    Finding,
    GitDiffError,
    _git_diff_against,
    is_allowlisted_path,
    is_env_file,
    is_placeholder_value,
    main,
    parse_diff_added_lines,
    scan_added_lines,
    scan_diff_text,
    scan_files_full_content,
    scan_line,
)


REPO_ROOT = Path(__file__).resolve().parent


def _reasons(line: str) -> list[str]:
    return [reason for reason, _snippet in scan_line(line)]


# --- Кожен префікс окремим тестом -------------------------------------------


def test_prefix_sk_ant():
    assert _reasons("key = sk-ant-api03-" + "a" * 40)


def test_prefix_sk_generic():
    assert _reasons("token: sk-" + "b" * 40)


def test_prefix_ghp():
    assert _reasons("auth ghp_" + "c" * 36)


def test_prefix_gho():
    assert _reasons("auth gho_" + "c" * 36)


def test_prefix_ghs():
    assert _reasons("auth ghs_" + "c" * 36)


def test_prefix_github_pat():
    assert _reasons("auth github_pat_" + "d" * 40)


def test_prefix_aiza_google():
    assert _reasons("gcp key = AIzaSy" + "E" * 33)


def test_prefix_slack_xoxb():
    assert _reasons("slack: xoxb-" + "1" * 20)


def test_prefix_slack_xoxa():
    assert _reasons("slack: xoxa-" + "1" * 20)


def test_prefix_slack_xoxp():
    assert _reasons("slack: xoxp-" + "1" * 20)


def test_prefix_slack_xoxr():
    assert _reasons("slack: xoxr-" + "1" * 20)


def test_prefix_slack_xoxs():
    assert _reasons("slack: xoxs-" + "1" * 20)


def test_prefix_gitlab_glpat():
    assert _reasons("gitlab: glpat-" + "f" * 20)


def test_prefix_aws_akia():
    assert _reasons("aws: AKIA" + "G" * 16)


def test_private_key_header():
    reasons = _reasons("-----BEGIN RSA PRIVATE KEY-----")
    assert any("приватного ключа" in r for r in reasons)


def test_short_word_starting_with_prefix_is_not_flagged():
    # "sky" починається на "sk" — не "sk-", тож взагалі не префікс.
    # "skip-intro" містить "sk" не "sk-" теж. Перевіряємо саму суть
    # анти-хибнопозитивної вимоги: короткий "хвіст" після префікса не рахується.
    assert _reasons("sk-oo") == []


# --- round 2 БЛОКЕР 1: таблиця "ловить / не ловить" (14 рядків) -------------
#
# 11 рядків з таблиці рев'ю (5 позитивних "ловить" + 5 негативних "не
# ловить" з реального production-коду цього репозиторію + метрика 48→0,
# перевірена окремо тестом test_no_false_positives_on_real_production_
# python_files) + 3 рядки доповнення Opus (lowercase-hex ключ, Fernet-ключ
# із "_", python-ідентифікатор з цифрою й підкресленням без hex-форми).

ROUND2_CATCH_TABLE = [
    # (рядок, чи МАЄ ловитись, короткий опис для id параметризації)
    pytest.param("BOT_TOKEN=8855895437:AAHdq" + "x" * 20, True, id="telegram-shaped-assignment"),
    pytest.param(
        'ANTHROPIC_API_KEY = "sk-ant-api03-' + "a" * 40 + '"', True, id="anthropic-key"
    ),
    pytest.param(
        'ENCRYPTION_KEY = "dGhpc2lz' + "A" * 30 + '=="', True, id="fernet-key-mixed-case"
    ),
    pytest.param(
        "TOMTOM_API_KEY=aBcD3fGh1jKlMnOpQrStUvWxYz012345", True, id="tomtom-key-mixed-case"
    ),
    pytest.param('POSTGRES_PASSWORD="Xy7qtR2mQp9vLs4wZn1k"', True, id="postgres-password"),
    pytest.param(
        'PRIVATE_KEY_HEADER_RE = re.compile("x"*25)', False, id="regex-compile-expression"
    ),
    pytest.param("token = _extract_token(line, idx)", False, id="function-call-expression"),
    pytest.param(
        "token_encrypted = await repo.get_operator_token(operator_id)",
        False,
        id="await-expression",
    ),
    pytest.param('api_key = os.getenv("GEMINI_API_KEY")', False, id="os-getenv-expression"),
    pytest.param("key_builder=location_key_builder,", False, id="bare-identifier-kwarg"),
    # +3 доповнення Opus (round 2, уточнення до БЛОКЕРА 1):
    pytest.param("OCM_KEY=a2af00" + "0" * 30, True, id="lowercase-hex-key-32plus"),
    pytest.param(
        "ENCRYPTION_KEY=abc_XYZ123_def456_ghi789_jkl0_1", True, id="fernet-like-with-underscore"
    ),
    pytest.param("token = api_client_v2", False, id="identifier-with-digit-and-underscore"),
]


@pytest.mark.parametrize("line, should_catch", ROUND2_CATCH_TABLE)
def test_round2_catch_table(line, should_catch):
    caught = bool(_reasons(line))
    assert caught == should_catch, f"{line!r}: очікували catch={should_catch}, отримали {caught}"


def test_short_lowercase_hex_below_32_not_flagged():
    # Причина порогу саме 32, не менше: Alembic revision id — 12 hex-символів
    # (`b1b193e2bd7b`), при порозі 32 вони не зачіпаються. Тут — 24 символи
    # (сенситивне імʼя, щоб дійти до самої hex-гілки правила).
    assert _reasons("API_SECRET=b1b193e2bd7bb1b193e2bd7b") == []


# --- ІМ'Я=значення для чутливих імен ----------------------------------------


@pytest.mark.parametrize("name", ["API_KEY", "BOT_TOKEN", "DB_SECRET", "ADMIN_PASSWORD"])
def test_sensitive_env_assignment_with_long_value(name):
    # Секрето-подібне значення: цифри + різний регістр (не голе слово).
    reasons = _reasons(f"{name}=AbCdEfGh1JkLmNoPqRsTuVwXyZ0123456789")
    assert any("чутливе присвоєння" in r for r in reasons)


def test_sensitive_name_with_short_value_not_flagged():
    assert _reasons("API_KEY=short") == []


def test_non_sensitive_name_with_long_value_not_flagged():
    assert _reasons("DESCRIPTION=abcdefghijklmnopqrstuvwxyz0123456789") == []


def test_env_assignment_empty_value_not_flagged():
    # Форма .env.example — значення порожнє.
    assert _reasons("ENCRYPTION_KEY=") == []


# --- Спроба додати сам .env --------------------------------------------------


def test_adding_env_file_is_flagged():
    findings = scan_added_lines(".env", [(1, "BOT_TOKEN=whatever")])
    assert any("спроба додати файл .env" in f.reason for f in findings)


def test_adding_nested_env_file_is_flagged():
    findings = scan_added_lines("config/.env", [(1, "X=1")])
    assert any("спроба додати файл .env" in f.reason for f in findings)


@pytest.mark.parametrize("suffix", ["prod", "local", "production", "staging"])
def test_adding_env_variant_files_is_flagged(suffix):
    # round 2, не-блокер 6: is_env_file раніше порівнював лише з точним ".env".
    findings = scan_added_lines(f".env.{suffix}", [(1, "X=1")])
    assert any("спроба додати файл .env" in f.reason for f in findings)


# --- round 2, не-блокер 3: позиційні секрети (не лише "NAME=") -------------


def test_telegram_token_positional_literal_is_flagged():
    line = 'bot = Bot("8855895437:AAHdq' + "x" * 35 + '")'
    reasons = _reasons(line)
    assert any("Telegram" in r for r in reasons)


def test_telegram_token_in_env_assignment_still_flagged():
    line = "BOT_TOKEN=8855895437:AAHdq" + "x" * 20
    assert _reasons(line) != []


def test_url_query_secret_positional_literal_is_flagged():
    line = 'url = f"https://api.tomtom.com/search?key=' + "a" * 25 + '"'
    reasons = _reasons(line)
    assert any("query-рядку" in r for r in reasons)


def test_url_query_secret_case_insensitive_param_name():
    line = "GET /oauth?Token=" + "b" * 25
    assert _reasons(line) != []


def test_positional_literal_without_recognized_shape_not_flagged():
    # Fernet-позиційний літерал (`Fernet("dGhpc2lz…==")`) свідомо ЛИШАЄТЬСЯ
    # непокритим цим раундом — round 2 просив рівно два нові патерни
    # (Telegram-токен, секрет у query URL), не загальний позиційний catch-all.
    line = 'cipher = Fernet("dGhpc2lz' + "A" * 30 + '==")'
    assert _reasons(line) == []


# --- round 3, Н1: YAML-форма "- NAME=value" (docker-compose.yml) -----------


def test_yaml_dash_form_assignment_is_flagged():
    line = "      - POSTGRES_PASSWORD=S3cr3tP4ssw0rdXyz123"
    assert _reasons(line) != []


def test_real_docker_compose_line_is_clean():
    # Реальний трекнутий рядок цього репозиторію (docker-compose.yml:26) —
    # ${...}-підстановка, не літеральний секрет. Не має ловитись.
    line = (
        "      - DB_URL=postgresql+asyncpg://${POSTGRES_USER}:"
        "${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB}"
    )
    assert _reasons(line) == []


# --- round 3, Н2: рядок підключення з паролем (незалежно від NAME) ---------


def test_connection_string_with_real_password_is_flagged():
    line = "DB_URL=postgresql://evolt:S3cr3tP4ssw0rd@db:5432/ev"
    reasons = _reasons(line)
    assert any("підключення" in r for r in reasons)


def test_connection_string_positional_without_sensitive_name_is_flagged():
    # DB_URL не містить KEY/TOKEN/SECRET/PASSWORD — "чутливе присвоєння"
    # на нього взагалі не дивиться, лише позиційний патерн Н2.
    line = 'url = "postgresql://evolt:S3cr3tP4ssw0rd@db:5432/ev"'
    assert _reasons(line) != []


@pytest.mark.parametrize(
    "line",
    [
        # Реальний .env.example (рядок 68) — CHANGE_ME нормалізується в
        # "change_me", уже в PLACEHOLDER_VALUES.
        "DB_URL=postgresql+asyncpg://ev_admin:CHANGE_ME@localhost:5432/ev_charge_base",
        # Реальний .github/workflows/ci.yml (рядок 112) — CI-фікстур, не секрет.
        "DB_URL: postgresql://ev_admin:ci_test_password@localhost:5432/ev_charge_base",
    ],
)
def test_connection_string_with_placeholder_password_not_flagged(line):
    assert _reasons(line) == []


# --- round 3, Н3: URL-константа під "чутливим" іменем не ловиться -----------


def test_url_constant_under_sensitive_name_not_flagged():
    line = 'MONOBANK_TOKEN_URL = "https://api.monobank.ua/api/merchant/details"'
    assert _reasons(line) == []


def test_url_constant_assignment_with_real_password_still_flagged():
    line = 'API_TOKEN_URL = "postgresql://evolt:S3cr3tP4ssw0rd@db:5432/ev"'
    assert _reasons(line) != []


def test_adding_env_example_is_not_flagged_as_env_file():
    assert is_env_file(".env.example") is False
    findings = scan_added_lines(".env.example", [(1, "BOT_TOKEN=")])
    assert findings == []


# --- Хибні спрацювання: allow-list шляхів -----------------------------------


def test_env_example_with_empty_values_is_clean():
    lines = [(1, "BOT_TOKEN="), (2, "ENCRYPTION_KEY="), (3, "OCM_KEY=")]
    assert scan_added_lines(".env.example", lines) == []


@pytest.mark.parametrize(
    "fake_value",
    ["fake-key", "encrypted-token", "test-token", "твій_ключ"],
)
def test_placeholder_values_recognized(fake_value):
    assert is_placeholder_value(fake_value) is True
    assert is_placeholder_value(f'"{fake_value}"') is True  # у лапках теж


def test_fake_tokens_in_test_files_not_flagged():
    lines = [
        (10, 'token = "fake-key-" + "x" * 40'),
        (11, "MONOBANK_TOKEN=encrypted-token-for-operator-5-abcxyz"),
    ]
    assert scan_added_lines("test_operator_payments.py", lines) == []


def test_mock_monobank_stub_not_flagged():
    lines = [(5, 'MONOBANK_TOKEN = "sk-ant-" + "q" * 40  # заглушка мока')]
    assert scan_added_lines("mock_monobank.py", lines) == []


def test_readme_not_flagged():
    lines = [(1, "ANTHROPIC_API_KEY=sk-ant-api03-" + "w" * 40)]
    assert scan_added_lines("README.md", lines) == []


def test_is_allowlisted_path_matches_expected_patterns():
    assert is_allowlisted_path("test_check_secrets.py") is True
    assert is_allowlisted_path("mock_monobank.py") is True
    assert is_allowlisted_path("mock_cpo.py") is True
    assert is_allowlisted_path("README.md") is True


def test_is_allowlisted_path_does_not_match_production_code():
    assert is_allowlisted_path("app/services/monobank_acquiring.py") is False
    assert is_allowlisted_path("auth_check.py") is False


# --- round 2, не-блокер 4: один секрет = один Finding ------------------------


def test_one_key_gives_one_finding_not_three():
    # `sk-ant-api03-…` раніше давав 3 окремих Finding (два префікси-підмножини
    # sk-ant-/sk- + "чутливе присвоєння") на ОДИН реальний секрет.
    line = 'key = "sk-ant-api03-' + "a" * 40 + '"'
    assert len(_reasons(line)) >= 2  # scan_line сам по собі знахідок не дедуплікує

    findings = scan_added_lines("auth_check.py", [(1, line)])
    assert len(findings) == 1
    assert findings[0].line_number == 1


def test_dedup_merges_reasons_and_keeps_first_snippet():
    line = 'key = "sk-ant-api03-' + "b" * 40 + '"'
    findings = scan_added_lines("auth_check.py", [(7, line)])
    assert len(findings) == 1
    f = findings[0]
    assert "sk-ant-" in f.reason
    assert "sk-" in f.reason
    assert f.snippet != ""


def test_dedup_keeps_distinct_findings_on_different_lines():
    lines = [
        (1, 'key = "sk-ant-api03-' + "c" * 40 + '"'),
        (2, 'other = "sk-ant-api03-' + "d" * 40 + '"'),
    ]
    findings = scan_added_lines("auth_check.py", lines)
    assert len(findings) == 2
    assert {f.line_number for f in findings} == {1, 2}


# --- round 2: .env.example і docs/*.md БІЛЬШЕ НЕ в allow-list ---------------
#
# Емпіричний тест (як для production-коду): прогнати реальний вміст усіх
# docs/*.md і .env.example і показати 0 хибних спрацювань — саме тому їх
# безпечно прибрати з ALLOWLISTED_PATH_PATTERNS (ховати шлях цілком, коли
# хибних спрацювань і так нуль, лише ховає майбутній реальний ключ).


def test_no_false_positives_on_real_docs_and_env_example():
    targets = list(REPO_ROOT.glob("docs/*.md")) + [REPO_ROOT / ".env.example"]
    assert len(targets) > 5  # санітарна перевірка вибірки

    all_findings = []
    for full_path in targets:
        rel_path = full_path.relative_to(REPO_ROOT).as_posix()
        text = full_path.read_text(encoding="utf-8", errors="replace")
        numbered = list(enumerate(text.splitlines(), start=1))
        all_findings.extend(scan_added_lines(rel_path, numbered))

    assert all_findings == [], (
        f"{len(all_findings)} хибних спрацювань у docs/.env.example: "
        f"{[(f.file, f.line_number, f.reason) for f in all_findings[:10]]}"
    )


def test_is_allowlisted_path_no_longer_matches_docs_or_env_example():
    assert is_allowlisted_path("docs/SESSION_STATE.md") is False
    assert is_allowlisted_path("docs/sub/notes.md") is False
    assert is_allowlisted_path(".env.example") is False


def test_real_secret_in_docs_now_caught():
    # Дірка, яку закриває цей раунд: раніше ключ, вставлений у docs/*.md,
    # був невидимий назавжди.
    lines = [(1, "Реальний ключ TomTom: AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ12345")]
    findings = scan_added_lines("docs/SESSION_STATE.md", lines)
    assert findings != []


def test_real_secret_in_env_example_now_caught():
    lines = [(1, "TOMTOM_API_KEY=aBcD3fGh1jKlMnOpQrStUvWxYz012345")]
    findings = scan_added_lines(".env.example", lines)
    assert findings != []


# --- Реальний секрет у production-коді ловиться -----------------------------


def test_real_looking_secret_in_app_code_is_flagged():
    lines = [(1, "ANTHROPIC_API_KEY = sk-ant-api03-" + "r" * 60)]
    findings = scan_added_lines("auth_check.py", lines)
    assert len(findings) >= 1
    assert all(f.file == "auth_check.py" for f in findings)


# --- ОБОВ'ЯЗКОВИЙ тест: сканер по РЕАЛЬНОМУ коду репозиторію -----------------
#
# Round 2 рев'ю (Opus): усі попередні тести ганяли сканер по СИНТЕТИЧНИХ
# рядках — жоден не проганяв його по реальному коду цього репозиторію, тому
# 48 хибних спрацювань на production-файлах і провал бандла на власному диффі
# лишились невидимими при повністю зелених тестах. Той самий клас проблеми,
# що PR #25 (writable-CTE) і TomTom 31.07: мок (тут — синтетичний рядок)
# повторював ті самі припущення, що й код.
#
# До фіксу правила "чутливе присвоєння" (round 2, БЛОКЕР 1) цей тест падає
# червоним ~48 знахідками. Після фіксу — зелений, 0 знахідок.


def test_no_false_positives_on_real_production_python_files():
    result = subprocess.run(
        ["git", "ls-files", "*.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    all_files = [f for f in result.stdout.splitlines() if f]
    production_files = [f for f in all_files if not is_allowlisted_path(f)]

    # Санітарна перевірка на саму вибірку — щоб порожній список файлів не
    # дав хибнозелений тест, якщо колись зміниться логіка is_allowlisted_path.
    assert len(production_files) > 50
    assert "app/services/monobank_acquiring.py" in production_files
    assert "auth_check.py" in production_files

    all_findings = []
    for rel_path in production_files:
        full_path = REPO_ROOT / rel_path
        try:
            text = full_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        numbered = list(enumerate(text.splitlines(), start=1))
        all_findings.extend(scan_added_lines(rel_path, numbered))

    assert all_findings == [], (
        f"{len(all_findings)} хибних спрацювань на production-коді: "
        f"{[(f.file, f.line_number, f.reason) for f in all_findings[:10]]}"
    )


def test_no_false_positives_on_all_tracked_files():
    """round 3 рев'ю: не лише `*.py` — усі 166 трекнутих файлів репозиторію
    (docker-compose.yml, .github/workflows/*, .env.example тощо), мінус
    allow-list. Саме тут спіймано і виправлено три реальних хибних
    спрацювання Н2/Н3 ДО того, як цей тест написано (docker-compose.yml:26
    ${...}-підстановка, .env.example:68 CHANGE_ME, ci.yml:112
    ci_test_password) — лишається зеленим ПІСЛЯ виправлення.
    """
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    all_files = [f for f in result.stdout.splitlines() if f]
    targets = [f for f in all_files if not is_allowlisted_path(f)]

    assert len(all_files) > 150  # санітарна перевірка — репозиторій не порожній
    assert len(targets) > 100

    all_findings = []
    for rel_path in targets:
        full_path = REPO_ROOT / rel_path
        try:
            text = full_path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError):
            continue  # бінарні файли (напр. favicon) — не наш формат
        numbered = list(enumerate(text.splitlines(), start=1))
        all_findings.extend(scan_added_lines(rel_path, numbered))

    assert all_findings == [], (
        f"{len(all_findings)} хибних спрацювань на {len(targets)} трекнутих файлах: "
        f"{[(f.file, f.line_number, f.reason) for f in all_findings[:10]]}"
    )


# --- parse_diff_added_lines: чистий парсер unified diff ---------------------


def test_parse_diff_added_lines_extracts_only_plus_lines_with_correct_numbers():
    diff_text = (
        "diff --git a/foo.py b/foo.py\n"
        "index 111..222 100644\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1,0 +2,2 @@\n"
        "+ANTHROPIC_API_KEY=sk-ant-api03-" + "s" * 40 + "\n"
        "+print(1)\n"
    )
    result = parse_diff_added_lines(diff_text)
    assert list(result.keys()) == ["foo.py"]
    assert result["foo.py"][0][0] == 2
    assert result["foo.py"][1][0] == 3


def test_parse_diff_added_lines_ignores_removed_lines():
    diff_text = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-old_secret = sk-ant-api03-" + "t" * 40 + "\n"
        "+new_line = 1\n"
    )
    result = parse_diff_added_lines(diff_text)
    contents = [line for _n, line in result["foo.py"]]
    assert contents == ["new_line = 1"]


def test_parse_diff_added_lines_skips_deleted_files():
    diff_text = (
        "diff --git a/gone.py b/gone.py\n"
        "deleted file mode 100644\n"
        "--- a/gone.py\n"
        "+++ /dev/null\n"
        "@@ -1,1 +0,0 @@\n"
        "-secret = sk-ant-api03-" + "u" * 40 + "\n"
    )
    result = parse_diff_added_lines(diff_text)
    assert result == {}


def test_scan_diff_text_end_to_end():
    diff_text = (
        "diff --git a/auth_check.py b/auth_check.py\n"
        "--- a/auth_check.py\n"
        "+++ b/auth_check.py\n"
        "@@ -1,0 +1,1 @@\n"
        "+key = \"sk-ant-api03-" + "v" * 40 + "\"\n"
    )
    findings = scan_diff_text(diff_text)
    assert any(f.file == "auth_check.py" for f in findings)


# --- Порожній список staged-файлів не ламає скрипт ---------------------------


def test_scan_files_full_content_empty_list_returns_empty():
    assert scan_files_full_content([]) == []


def test_main_with_no_args_and_no_diff_exits_zero(monkeypatch):
    monkeypatch.setattr("scripts.check_secrets._git_diff_cached", lambda: "")
    assert main([]) == 0


def test_main_with_empty_diff_text_exits_zero(monkeypatch):
    monkeypatch.setattr("scripts.check_secrets._git_diff_cached", lambda: "\n")
    assert main([]) == 0


# --- round 3 БЛОКЕР: "git впав" ≠ "диф порожній" — fail-closed, не fail-open


def test_git_diff_against_unreachable_ref_raises_git_diff_error():
    with pytest.raises(GitDiffError):
        _git_diff_against("deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")


def test_main_with_unreachable_base_ref_returns_1_not_0(capsys):
    # ДО фіксу цей сценарій давав exit 0 (git exit=128 ігнорувався,
    # result.stdout порожній -> scan_diff_text("") -> 0 знахідок) — той
    # самий клас невидимості, що вже двічі спливав у цьому бандлі.
    exit_code = main(["--base", "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.err != ""


def test_main_with_unreachable_base_ref_does_not_print_full_git_stderr(capsys):
    # Не друкувати сирий stderr git ("fatal: ...") — лише факт збою і команду.
    main(["--base", "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"])
    captured = capsys.readouterr()
    assert "fatal:" not in captured.err.lower()


# --- CLI: код виходу і що саме друкується ------------------------------------


def test_main_returns_1_and_prints_finding_for_real_secret(tmp_path, capsys):
    bad_file = tmp_path / "leaky.py"
    secret = "sk-ant-api03-" + "y" * 60
    bad_file.write_text(f'key = "{secret}"\n', encoding="utf-8")

    exit_code = main([str(bad_file)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert secret not in captured.err  # повний секрет НІКОЛИ не друкується
    assert secret[:4] in captured.err  # лише перші символи


def test_main_returns_0_for_allowlisted_file_with_fake_secret(tmp_path):
    test_file = tmp_path / "test_something.py"
    test_file.write_text('token = "fake-key-abcdefghijklmnopqrstuvwxyz"\n', encoding="utf-8")

    assert main([str(test_file)]) == 0


def test_main_returns_0_for_clean_file(tmp_path):
    clean_file = tmp_path / "clean.py"
    clean_file.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    assert main([str(clean_file)]) == 0


def test_main_with_missing_file_does_not_crash(tmp_path):
    missing = tmp_path / "does_not_exist.py"
    assert main([str(missing)]) == 0


# --- Наскрізна перевірка через реальний git (не лише парсер рядків) ---------


def _run(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True)


def test_git_diff_cached_integration_catches_staged_secret(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init"], cwd=repo)
    _run(["git", "config", "user.email", "t@example.com"], cwd=repo)
    _run(["git", "config", "user.name", "t"], cwd=repo)

    leak_file = repo / "auth_check.py"
    leak_file.write_text("x = 1\n", encoding="utf-8")
    _run(["git", "add", "auth_check.py"], cwd=repo)
    _run(["git", "commit", "-m", "init"], cwd=repo)

    secret = "sk-ant-api03-" + "z" * 60
    leak_file.write_text(f'key = "{secret}"\n', encoding="utf-8")
    _run(["git", "add", "auth_check.py"], cwd=repo)

    script = str((__file__.replace("test_check_secrets.py", "")) + "scripts/check_secrets.py")
    result = subprocess.run(
        [sys.executable, script],
        cwd=repo,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert secret not in result.stdout
    assert secret not in result.stderr


def test_git_diff_base_ref_integration_catches_secret_in_branch(tmp_path):
    # Той самий режим, що використовує CI на диффі PR: --base <ref> замість
    # --cached (staged-диф тут узагалі не задіяний — коміт уже зроблений).
    repo = tmp_path / "repo_base"
    repo.mkdir()
    _run(["git", "init", "-b", "main"], cwd=repo)
    _run(["git", "config", "user.email", "t@example.com"], cwd=repo)
    _run(["git", "config", "user.name", "t"], cwd=repo)

    base_file = repo / "app.py"
    base_file.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    _run(["git", "add", "app.py"], cwd=repo)
    _run(["git", "commit", "-m", "init"], cwd=repo)

    _run(["git", "checkout", "-b", "feature/leak"], cwd=repo)
    secret = "sk-ant-api03-" + "b" * 60
    (repo / "auth_check.py").write_text(f'key = "{secret}"\n', encoding="utf-8")
    _run(["git", "add", "auth_check.py"], cwd=repo)
    _run(["git", "commit", "-m", "oops"], cwd=repo)

    script = str((__file__.replace("test_check_secrets.py", "")) + "scripts/check_secrets.py")
    result = subprocess.run(
        [sys.executable, script, "--base", "main"],
        cwd=repo,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert secret not in result.stdout
    assert secret not in result.stderr


def test_git_diff_cached_integration_clean_repo_exits_zero(tmp_path):
    repo = tmp_path / "repo_clean"
    repo.mkdir()
    _run(["git", "init"], cwd=repo)
    _run(["git", "config", "user.email", "t@example.com"], cwd=repo)
    _run(["git", "config", "user.name", "t"], cwd=repo)

    ok_file = repo / "app.py"
    ok_file.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    _run(["git", "add", "app.py"], cwd=repo)

    script = str((__file__.replace("test_check_secrets.py", "")) + "scripts/check_secrets.py")
    result = subprocess.run(
        [sys.executable, script],
        cwd=repo,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
