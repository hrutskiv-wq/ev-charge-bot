import anthropic
import os

key = "ТВІЙ_СТАРИЙ_КЛЮЧ"
print(f"Довжина ключа: {len(key)}")
print(f"Перші 5 символів: {key[:5]}")

try:
    client = anthropic.Anthropic(api_key=key)
    # Запит до моделей
    client.models.list()
    print("Авторизація успішна!")
except Exception as e:
    print(f"Помилка авторизації: {e}")
EOF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