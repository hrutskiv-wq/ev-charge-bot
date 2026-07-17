"""
Тести на app/api/ocpi.py::verify_ocpi_token — авторизацію вхідних OCPI-запитів
(CDR, callback команд). До фіксу цей ендпоінт був повністю відкритим: будь-хто
міг надіслати фейковий CDR і списати/нарахувати кошти довільному користувачу.

Запуск: pytest test_ocpi_auth.py -v
"""
import pytest
from fastapi import HTTPException

from app.api.ocpi import verify_ocpi_token
from app.services.ocpi.config import OCPIConfig


async def test_missing_authorization_header_rejected():
    """Без заголовка Authorization запит має відхилятись з 401."""
    with pytest.raises(HTTPException) as exc_info:
        await verify_ocpi_token(authorization=None)
    assert exc_info.value.status_code == 401


async def test_wrong_token_rejected():
    """Заголовок присутній, але токен не збігається з OCPI_SECRET_TOKEN — 401."""
    with pytest.raises(HTTPException) as exc_info:
        await verify_ocpi_token(authorization="Token не-той-токен")
    assert exc_info.value.status_code == 401


async def test_correct_token_missing_prefix_rejected():
    """Токен правильний за значенням, але без префікса 'Token ' — теж 401
    (порівнюється весь заголовок цілком, а не лише значення токена)."""
    with pytest.raises(HTTPException) as exc_info:
        await verify_ocpi_token(authorization=OCPIConfig.OCPI_TOKEN)
    assert exc_info.value.status_code == 401


async def test_correct_token_accepted():
    """З правильним заголовком 'Token <OCPI_SECRET_TOKEN>' запит проходить
    (функція нічого не повертає і не кидає виключення)."""
    result = await verify_ocpi_token(authorization=f"Token {OCPIConfig.OCPI_TOKEN}")
    assert result is None
