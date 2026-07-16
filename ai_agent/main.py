import os
from crewai import Agent, Crew, Process, Task
from langchain_anthropic import ChatAnthropic
from dotenv import load_dotenv

# Завантажуємо змінні з .env, який лежить у корені проєкту EV_CHARGE_BOT
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '../.env'))

api_key = os.getenv("ANTHROPIC_API_KEY")

if not api_key:
    raise ValueError("❌ Помилка: ANTHROPIC_API_KEY не знайдено у вашому .env файлі!")

# Ініціалізуємо найновішу модель Claude Sonnet 5 (реліз 2026 року)
llm = ChatAnthropic(
    model="claude-sonnet-5",
    api_key=api_key,
    )

# 1. Агент-Архітектор
architect = Agent(
    role="e-Mobility Solutions Architect",
    goal="Проектувати надійну та масштабовану архітектуру eMSP сервісів згідно з OCPI 2.2.1.",
    backstory=(
        "Ти — експерт з EV-інфраструктури та OCPI 2.2.1. Твоє завдання — розробляти точні "
        "схеми баз даних PostgreSQL та описувати алгоритми для розробників."
    ),
    verbose=True,
    llm=llm
)

# 2. Агент-Розробник
developer = Agent(
    role="Senior Async Python Developer",
    goal="Писати чистий, суворо асинхронний Python-код для FastAPI та Aiogram 3.x.",
    backstory=(
        "Ти розробляєш проєкт EV_CHARGE_BOT. Ти пишеш код відповідно до таких суворих правил:\n"
        "1. Тільки асинхронний код (async/await).\n"
        "2. Жодних прямих імпортів бота в API! Тільки через request.app.state.bot.\n"
        "3. Звернення до пулу БД: `from app.database import connection -> connection.db_pool.acquire()`.\n"
        "4. Фінансові транзакції (kw_transactions) обертаються в 'async with conn.transaction()'."
    ),
    verbose=True,
    llm=llm
)

# Завдання для запуску
task_design = Task(
    description=(
        "Спроектуй структуру таблиць для збереження інформації про зарядні станції партнерів "
        "(модуль Locations) згідно з OCPI 2.2.1. Потрібні сутності Locations, EVSEs, Connectors."
    ),
    expected_output="SQL-схема PostgreSQL та опис зв'язків між таблицями.",
    agent=architect
)

task_code = Task(
    description=(
        "На основі схеми від архітектора, напиши код асинхронного FastAPI роутера "
        "для обробки вхідних локацій від CPO, а також шаблон міграції Alembic."
    ),
    expected_output="Готовий Python-код для FastAPI та файл міграції.",
    agent=developer
)

crew = Crew(
    agents=[architect, developer],
    tasks=[task_design, task_code],
    process=Process.sequential,
    verbose=True
)

if __name__ == "__main__":
    print("🚀 AI-агенти починають розробку модуля Locations на базі Claude Sonnet 5...")
    result = crew.kickoff()
    print("\n🎯 РЕЗУЛЬТАТ РОБОТИ:")
    print(result)
