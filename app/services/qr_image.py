"""
Генерація QR-PNG для станцій оператора (майстер станції, Промпт 4).

Окремий модуль, а не інлайн у хендлері — щоб handler-код не знав нічого про
бібліотеку `qrcode` і його можна було покрити тестом без Telegram/aiogram.
"""
import io

import qrcode


def generate_station_qr_png(url: str) -> bytes:
    """PNG-байти QR-коду на переданий URL, готові для answer_photo/answer_document."""
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
