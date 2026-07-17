"""
Тести на app/api/payments.py::_verify_monobank_signature — перевірку ECDSA
підпису вебхука Monobank (заголовок x-sign). До фіксу цей заголовок взагалі
не перевірявся: будь-хто, хто знав URL вебхука, міг надіслати підроблене
"успішне поповнення" з довільним Telegram ID у comment і безкоштовно
нарахувати собі кВт·год.

Тут генерується власна тестова ECDSA-пара (P-256/SHA-256 — той самий
алгоритм, що й у Monobank), і app._fetch_monobank_pubkey підміняється, щоб
повертати публічну частину цієї пари — так перевіряється сама логіка
верифікації (cryptography.hazmat...ec.ECDSA(SHA256)), без залежності від
живого API Monobank чи реального токена мерчанта.

Запуск: pytest test_monobank_signature.py -v
"""
import base64

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

import app.api.payments as payments


@pytest.fixture
def keypair():
    private_key = ec.generate_private_key(ec.SECP256R1())
    return private_key, private_key.public_key()


def _sign(private_key, body: bytes) -> str:
    signature = private_key.sign(body, ec.ECDSA(hashes.SHA256()))
    return base64.b64encode(signature).decode()


@pytest.fixture(autouse=True)
def patch_pubkey_fetch(monkeypatch, keypair):
    """Підміняємо мережевий виклик до Monobank публічним ключем нашої тестової пари."""
    _, public_key = keypair

    async def fake_fetch(force_refresh: bool = False):
        return public_key

    monkeypatch.setattr(payments, "_fetch_monobank_pubkey", fake_fetch)


async def test_valid_signature_accepted(keypair):
    private_key, _ = keypair
    body = b'{"type":"StatementItem","data":{"statementItem":{"id":"abc","amount":75000}}}'
    x_sign = _sign(private_key, body)

    assert await payments._verify_monobank_signature(body, x_sign) is True


async def test_tampered_body_rejected(keypair):
    """Підпис валідний для оригінального тіла, але тіло підмінили після підпису
    (наприклад, збільшили суму) — верифікація має провалитись."""
    private_key, _ = keypair
    original_body = b'{"type":"StatementItem","data":{"statementItem":{"id":"abc","amount":75000}}}'
    x_sign = _sign(private_key, original_body)

    tampered_body = original_body.replace(b'"amount":75000', b'"amount":9999999')
    assert await payments._verify_monobank_signature(tampered_body, x_sign) is False


async def test_signature_from_wrong_key_rejected(keypair):
    """Підпис зроблено іншим (не Monobank) приватним ключем — маскарад під Monobank."""
    body = b'{"type":"StatementItem","data":{"statementItem":{"id":"abc","amount":75000}}}'
    attacker_key = ec.generate_private_key(ec.SECP256R1())
    forged_sign = _sign(attacker_key, body)

    assert await payments._verify_monobank_signature(body, forged_sign) is False


async def test_missing_signature_header_rejected(keypair):
    body = b'{"type":"StatementItem"}'
    assert await payments._verify_monobank_signature(body, None) is False
    assert await payments._verify_monobank_signature(body, "") is False


async def test_malformed_base64_signature_rejected(keypair):
    body = b'{"type":"StatementItem"}'
    assert await payments._verify_monobank_signature(body, "не-base64-!!!") is False
