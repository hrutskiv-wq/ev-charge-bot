import os
import time
from crewai import Agent, Crew, Task
from crewai_tools import FileReadTool, FileWriterTool

# Очищення
if "OPENAI_API_KEY" in os.environ: del os.environ["OPENAI_API_KEY"]

# Перемикаємося на Haiku - вона надійніша при навантаженнях
llm_model = "anthropic/claude-haiku-4-5-20251001"

print(f"--- Запуск з моделлю {llm_model} ---")

# Агенти
refactorer = Agent(role="Senior Python Engineer", goal="Refactor code", backstory="Expert.", llm=llm_model, tools=[FileReadTool()])
auditor = Agent(role="OCPI Compliance Auditor", goal="Check compliance", backstory="Expert.", llm=llm_model, tools=[FileWriterTool()])

# Завдання
task1 = Task(description="Analyze app/api/ocpi.py", expected_output="Optimized code.", agent=refactorer)
task2 = Task(description="Verify and save.", expected_output="Final file.", agent=auditor)

crew = Crew(agents=[refactorer, auditor], tasks=[task1, task2])

# Запуск
try:
    print(crew.kickoff())
except Exception as e:
    print(f"Помилка при запуску: {e}")
