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
    """Покроковий майстер додавання станції. Адреса/конектор/потужність/старт — опційні (`-` пропускає)."""
    waiting_for_name = State()
    waiting_for_address = State()
    waiting_for_connector = State()
    waiting_for_power = State()
    waiting_for_tariff_kwh = State()
    waiting_for_tariff_start = State()


class TariffEdit(StatesGroup):
    waiting_for_new_tariff = State()


# Усі стани модуля разом — використовується там, де треба відрізнити "ми в
# кабінеті оператора" від довільного стану іншого розділу бота (наприклад,
# щоб /start і /operator гарантовано скидали лише СВІЙ стан, а не губили
# чужий чужого розділу випадково).
ALL_STATES = (
    OperatorOnboarding.waiting_for_name, OperatorOnboarding.waiting_for_phone,
    MonobankConnect.waiting_for_token,
    StationWizard.waiting_for_name, StationWizard.waiting_for_address,
    StationWizard.waiting_for_connector, StationWizard.waiting_for_power,
    StationWizard.waiting_for_tariff_kwh, StationWizard.waiting_for_tariff_start,
    TariffEdit.waiting_for_new_tariff,
)
