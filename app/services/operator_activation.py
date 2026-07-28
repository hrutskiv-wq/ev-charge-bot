"""
Чек-лист прогресу й автоактивація оператора (самообслуговуваний онбординг).

Три об'єктивні, перевірювані критерії — pending -> active БЕЗ жодної дії
адміна, щойно всі три виконано:

  1. профіль заповнено (назва й телефон — уже є на момент реєстрації,
     create_operator() вимагає обидва до запису рядка);
  2. токен Monobank підключено І ПІДТВЕРДЖЕНО реальним зверненням до банку
     (app/services/monobank_acquiring.py::verify_merchant_token()) — не
     просто збережений рядок;
  3. створено щонайменше одну станцію.

Модель довіри перевернута відносно ручної модерації Промпту 4: замість
"за замовчуванням заборонено, адмін дозволяє" — "дозволено після проходження
перевірок, адмін може відключити" (suspend, app/handlers/operator_billing.py
— головний запобіжник цієї моделі). Ручний шлях (admin_activate_operator)
НЕ прибирається — лишається запасним для випадків, коли автоматика з
якоїсь причини не спрацювала або адмін хоче форсувати негайно.

is_public (публічна видимість у водійському пошуку) — ОКРЕМА від активації
межа безпеки, свідомо НЕ входить до критеріїв тут: активація (навіть
автоматична) ніколи сама не робить станції публічними, це вирішується
вручну (repo.set_operator_public), не в цьому модулі.
"""
from dataclasses import dataclass

from app.database import operators_repo as repo


@dataclass
class OperatorChecklist:
    """
    profile_complete/has_station — прямо з даних. has_token окремо від
    token_verified: перше лише "рядок збережено", друге — "банк підтвердив
    САМЕ це значення" (set_operator_monobank_token скидає token_verified у
    NULL при кожному новому токені). Розрізнення потрібне UI (checklist_
    keyboard): "підключити" показуємо, коли токена взагалі нема, "перевірити
    ще раз" — коли є, але ще не підтверджений.
    """
    profile_complete: bool
    has_token: bool
    token_verified: bool
    has_station: bool

    @property
    def ready(self) -> bool:
        return self.profile_complete and self.token_verified and self.has_station


async def get_checklist(operator_id: int) -> OperatorChecklist:
    operator = await repo.get_operator(operator_id)
    if operator is None:
        return OperatorChecklist(False, False, False, False)

    token_encrypted = await repo.get_operator_monobank_token_encrypted(operator_id)
    stations = await repo.list_stations(operator_id)

    return OperatorChecklist(
        profile_complete=bool(operator["name"]) and bool(operator["phone"]),
        has_token=bool(token_encrypted),
        token_verified=operator["monobank_token_verified_at"] is not None,
        has_station=len(stations) > 0,
    )


async def try_auto_activate(operator_id: int) -> bool:
    """
    Повертає True лише якщо САМЕ ЦЕЙ виклик активував оператора (для
    рішення, чи слати сповіщення). Ідемпотентно й безпечно при паралельних
    викликах: якщо оператор уже не 'pending' — миттєвий False без запиту
    чек-листа; сама зміна статусу — атомарний UPDATE ... WHERE
    status='pending' у repo.activate_operator_if_pending(), тож навіть два
    майже одночасні тригери (напр. верифікація токена і створення станції
    в паралельних запитах) активують оператора рівно один раз.
    """
    operator = await repo.get_operator(operator_id)
    if operator is None or operator["status"] != "pending":
        return False

    checklist = await get_checklist(operator_id)
    if not checklist.ready:
        return False

    return await repo.activate_operator_if_pending(operator_id)
