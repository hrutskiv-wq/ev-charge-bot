"""
Тести на прапорець ENABLE_API_DOCS (app/main.py) — /docs, /redoc,
/openapi.json вимкнені за замовчуванням (закриває живу знахідку 03.08.2026:
/openapi.json на проді віддавав повний перелік ендпоінтів, включно з
грошовими вебхуками).

`docs_url`/`redoc_url`/`openapi_url` читаються ОДИН РАЗ при конструюванні
FastAPI(...) — просту зміну env-змінної й повторний імпорт у ТОМУ САМОМУ
процесі тут не застосувати: `importlib.reload(app.main)` повторно виконує
`dp.include_router(...)` для вже приєднаних aiogram-роутерів і падає
("Router is already attached"), лишаючи спільний `dp` (app.core.loader) у
зіпсованому стані для решти тестів цього ж прогону pytest — саме тому
кожен сценарій тут запускається в ОКРЕМОМУ процесі (той самий підхід, що
git-інтеграційні тести в test_check_secrets.py).

Запуск: pytest test_api_docs.py -v
"""
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

_CHECK_SCRIPT = (
    "from fastapi.testclient import TestClient\n"
    "import app.main as m\n"
    "client = TestClient(m.app)\n"
    "import json\n"
    "print(json.dumps({\n"
    "    'openapi': client.get('/openapi.json').status_code,\n"
    "    'docs': client.get('/docs').status_code,\n"
    "    'redoc': client.get('/redoc').status_code,\n"
    "    'health': client.get('/health').status_code,\n"
    "}))\n"
)


def _run_app_in_subprocess(enable_api_docs: str | None) -> dict:
    env = os.environ.copy()
    if enable_api_docs is None:
        # НЕ env.pop(...): app/core/loader.py викликає load_dotenv() (default
        # override=False) — pop лишає ім'я відсутнім у середовищі дочірнього
        # процесу, і якщо в РЕАЛЬНОМУ .env розробника колись з'явиться
        # ENABLE_API_DOCS=1 (.env.example саме це й підказує для локальної
        # розробки), load_dotenv() підхопить його звідти, і цей тест почне
        # падати без жодної зміни коду. Порожній рядок — ім'я вже "зайняте"
        # в оточенні, load_dotenv() його не перезапише, гілка "не '1'" та
        # сама, а тест лишається герметичним незалежно від вмісту .env.
        env["ENABLE_API_DOCS"] = ""
    else:
        env["ENABLE_API_DOCS"] = enable_api_docs
    # Ті самі плейсхолдери, що CI (.github/workflows/ci.yml) — обов'язкові
    # для імпорту app.main (OCPI-конфіг/Bot-токен валідуються при імпорті).
    env.setdefault("OCPI_SECRET_TOKEN", "ci-placeholder-token")
    env.setdefault("BOT_TOKEN", "123456:ci-placeholder-bot-token")
    env.setdefault("GEMINI_API_KEY", "ci-placeholder-gemini-key")

    result = subprocess.run(
        [sys.executable, "-c", _CHECK_SCRIPT],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"дочірній процес упав:\n{result.stdout}\n{result.stderr}"
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_docs_disabled_by_default():
    statuses = _run_app_in_subprocess(None)

    assert statuses["openapi"] == 404
    assert statuses["docs"] == 404
    assert statuses["redoc"] == 404
    assert statuses["health"] in (200, 503)  # живий, не 404/500 — не зачепили інші роути


def test_docs_enabled_with_flag():
    statuses = _run_app_in_subprocess("1")

    assert statuses["openapi"] == 200
    assert statuses["docs"] == 200
    assert statuses["redoc"] == 200
    assert statuses["health"] in (200, 503)  # живий, не 404/500
