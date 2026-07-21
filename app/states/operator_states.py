"""FSM-стани кабінету оператора (Промпт 4)."""
from aiogram.fsm.state import State, StatesGroup


class OperatorOnboarding(StatesGroup):
    """Реєстрація нового оператора: назва -> телефон -> запис у operators (pending)."""
    waiting_for_name = State()
    waiting_for_phone = State()


class MonobankConnect(StatesGroup):
    """Підключення еквайрингу: один крок, токен приймається лише в приватному чаті."""
    waiting_for_token = State()


class StationWizard(StatesGroup):
    """
    Покроковий майстер додавання станції. Адреса/локація/потужність/старт —
    опційні (`-` пропускає); конектор — кнопками (Промпт 4c), фактично
    обов'язковий (без "Пропустити"), бо саме він разом із потужністю формує
    бейдж швидкості станції у водійському пошуку.
    """
    waiting_for_name = State()
    waiting_for_address = State()
    waiting_for_location = State()
    waiting_for_connector = State()
    waiting_for_power = State()
    waiting_for_tariff_kwh = State()
    waiting_for_tariff_start = State()


class TariffEdit(StatesGroup):
    waiting_for_new_tariff = State()
