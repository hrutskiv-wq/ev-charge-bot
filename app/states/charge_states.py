from aiogram.fsm.state import StatesGroup, State

class ChargingStates(StatesGroup):
    choosing_connector = State()  # Водій ввів ID станції і зараз обирає кабель
    charging_active = State()     # Кабель підключено, йде активна зарядка