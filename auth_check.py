import anthropic
import os
key = os.getenv("ANTHROPIC_API_KEY")
print(f"Довжина ключа: {len(key)}")
print(f"Перші 5 символів: {key[:5]}")

try:
    client = anthropic.Anthropic(api_key=key)
    # Запит до моделей
    client.models.list()
    print("Авторизація успішна!")
except Exception as e:
    print(f"Помилка авторизації: {e}")