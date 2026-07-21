"""
Шифрування секретів операторів (Fernet на ENCRYPTION_KEY).

Навіщо: у білінгу ми зберігаємо еквайринг-токен Monobank КОЖНОГО оператора
(operators.monobank_token_encrypted). Це чужі гроші: з таким токеном можна
створювати інвойси й читати виписку мерчанта. У базі він має лежати лише
зашифрованим, щоб дамп бази чи бекап не був одразу компрометацією всіх
операторів.

Fernet (AES-128-CBC + HMAC-SHA256) обрано, бо він уже доступний — пакет
`cryptography` у прод-залежностях (використовується для ECDSA-перевірки
webhook Monobank) — і не дає зібрати «свою криптографію» неправильно.

ПОВЕДІНКА БЕЗ КЛЮЧА — навмисно ледача. Ключ потрібен лише білінгу
операторів; решта застосунку (бот, OCPI, поповнення балансу водіїв) від
нього не залежить. Тому відсутній ENCRYPTION_KEY НЕ валить старт процесу —
інакше кожен уже розгорнутий інстанс перестав би підніматись після
деплою цього коду. Замість цього: гучний WARNING при старті
(warn_if_key_missing(), викликається з lifespan) і RuntimeError у момент,
коли шифрування реально знадобилось.

Згенерувати ключ:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
і покласти в .env як ENCRYPTION_KEY=...

УВАГА: зміна ENCRYPTION_KEY робить усі вже збережені токени
нерозшифровуваними. Ротація ключа = перешифрування всіх рядків
operators.monobank_token_encrypted, окремою процедурою; просто підмінити
значення в .env не можна.
"""
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

ENV_VAR = "ENCRYPTION_KEY"

_MISSING_KEY_MESSAGE = (
    f"{ENV_VAR} не задано — білінг операторів непрацездатний до додавання ключа. "
    "Згенеруйте: python -c \"from cryptography.fernet import Fernet; "
    "print(Fernet.generate_key().decode())\" і додайте в .env."
)

# Кеш екземпляра Fernet: розбір ключа коштує помітно дорожче за саме
# шифрування, а ключ за час життя процесу не змінюється.
_fernet = None


class EncryptionKeyMissing(RuntimeError):
    """ENCRYPTION_KEY не заданий, а код дійшов до місця, де він потрібен."""


def is_configured() -> bool:
    """Чи заданий ключ. Дозволяє шару вище коректно відмовити ДО спроби."""
    return bool(os.getenv(ENV_VAR))


def warn_if_key_missing():
    """
    Гучне попередження при старті застосунку. Викликається з lifespan —
    щоб відсутність ключа була видно одразу в логах, а не тільки тоді, коли
    перший оператор спробує підключити еквайринг.
    """
    if not is_configured():
        logger.warning("⚠️ %s", _MISSING_KEY_MESSAGE)
        return False
    return True


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is not None:
        return _fernet

    key = os.getenv(ENV_VAR)
    if not key:
        raise EncryptionKeyMissing(_MISSING_KEY_MESSAGE)

    try:
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    except Exception as e:
        raise EncryptionKeyMissing(
            f"{ENV_VAR} задано, але це не валідний Fernet-ключ ({e}). "
            "Потрібен рівно той рядок, що друкує Fernet.generate_key()."
        ) from e
    return _fernet


def encrypt_secret(plaintext: str) -> str:
    """Шифрує секрет для зберігання в БД. Повертає ASCII-рядок."""
    if not plaintext:
        raise ValueError("Порожній секрет шифрувати немає сенсу")
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(token: str) -> str:
    """
    Розшифровує секрет із БД.

    InvalidToken тут майже завжди означає одне з двох: ENCRYPTION_KEY
    підмінили (і старі записи більше не читаються) або в колонку потрапив
    незашифрований рядок. Обидва випадки — привід зупинитись, а не
    продовжити з «якимось» значенням, тому помилка не глушиться.
    """
    if not token:
        raise ValueError("Порожній токен розшифрувати неможливо")
    try:
        return _get_fernet().decrypt(token.encode()).decode()
    except InvalidToken as e:
        raise EncryptionKeyMissing(
            "Не вдалося розшифрувати секрет: значення не відповідає поточному "
            f"{ENV_VAR}. Ключ змінювали без перешифрування збережених токенів?"
        ) from e


def reset_cache():
    """Скидає кеш Fernet. Потрібно лише тестам, які підміняють ключ."""
    global _fernet
    _fernet = None
