"""
Тести об'єднаного пошуку станцій у app/handlers/user.py (Промпт 4c):
змішана видача OCM + White-Label операторських станцій.

_merge_search_results / _format_*_station_card — чисті функції (без
Telegram/БД), тому тестуються напряму, без фейкового бота чи диспетчера.

Запуск: pytest test_user_station_search.py -v
"""
from app.handlers import user as user_handlers

OPERATOR_STATION = {
    "id": 10, "operator_id": 1, "name": "Готель Едем", "distance_km": 1.2,
    "power_kw": 22.0, "connector_type": "Type 2", "tariff_uah_kwh": 12.5,
    "qr_slug": "abc123",
}

OCM_STATION = {
    "id": "OCM-999", "name": "Зубра HyperCharger", "address": "Зубра, 1",
    "distance": 3.4, "operator": "Go ToU", "connectors": "CCS (240 кВт) x2",
    "lat": 49.79, "lon": 23.95, "power_kw": 240, "connector_type": "CCS (Type 2)",
}


# ---------------------------------------------------------------------------
# _merge_search_results
# ---------------------------------------------------------------------------

def test_merge_returns_empty_list_for_no_stations():
    assert user_handlers._merge_search_results([], []) == []
    assert user_handlers._merge_search_results(None, None) == []


def test_merge_sorts_mixed_sources_by_distance():
    near_operator = {**OPERATOR_STATION, "distance_km": 1.2}
    far_ocm = {**OCM_STATION, "distance": 3.4}
    near_ocm = {**OCM_STATION, "id": "OCM-1", "distance": 0.5}

    result = user_handlers._merge_search_results([far_ocm, near_ocm], [near_operator])

    assert [item["source"] for item in result] == ["ocm", "operator", "ocm"]
    assert [item["distance_km"] for item in result] == [0.5, 1.2, 3.4]


def test_merge_with_only_operator_stations():
    result = user_handlers._merge_search_results([], [OPERATOR_STATION])
    assert len(result) == 1
    assert result[0]["source"] == "operator"
    assert result[0]["station"] is OPERATOR_STATION


def test_merge_with_only_ocm_stations():
    result = user_handlers._merge_search_results([OCM_STATION], [])
    assert len(result) == 1
    assert result[0]["source"] == "ocm"
    assert result[0]["station"] is OCM_STATION


def test_merge_puts_ocm_station_without_distance_at_the_end():
    no_distance = {**OCM_STATION, "distance": None}
    near_operator = {**OPERATOR_STATION, "distance_km": 5.0}

    result = user_handlers._merge_search_results([no_distance], [near_operator])

    assert [item["source"] for item in result] == ["operator", "ocm"]


# ---------------------------------------------------------------------------
# Формат карток
# ---------------------------------------------------------------------------

def test_operator_station_card_includes_badge_tariff_and_qr_link():
    text = user_handlers._format_operator_station_card(1, OPERATOR_STATION)

    assert "🐢 Повільна (AC)" in text  # 22 кВт — межа: <= 22 це повільна, а не середня
    assert "Готель Едем" in text
    assert "1.20 км" in text
    assert "22.0 кВт" in text
    assert "Type 2" in text
    assert "12.5 грн/кВт·год" in text
    assert f"{user_handlers.PUBLIC_BASE_URL}/s/abc123" in text


def test_operator_station_card_badge_matches_classify_function():
    from app.services.station_speed import classify_station_speed

    expected_badge = classify_station_speed(OPERATOR_STATION["power_kw"], OPERATOR_STATION["connector_type"])
    text = user_handlers._format_operator_station_card(1, OPERATOR_STATION)
    assert text.startswith(expected_badge)


def test_operator_station_card_omits_missing_power_and_connector_lines():
    minimal = {**OPERATOR_STATION, "power_kw": None, "connector_type": None}
    text = user_handlers._format_operator_station_card(1, minimal)

    assert "Потужність" not in text
    assert "Конектор" not in text


def test_ocm_station_card_keeps_legacy_fields_and_adds_badge_prefix():
    text = user_handlers._format_ocm_station_card(2, OCM_STATION)

    assert text.startswith("⚡ Швидка (DC)")  # 240 кВт -> швидка
    assert "Зубра HyperCharger" in text
    assert "Go ToU" in text
    assert "3.40 км" in text
    assert "CCS (240 кВт) x2" in text
    assert "OCM-999" in text


# ---------------------------------------------------------------------------
# Екранування HTML у полях, які ввів оператор (рев'ю Промпту 4c)
# ---------------------------------------------------------------------------

def test_operator_station_card_escapes_html_in_name_and_connector():
    """
    Назву й конектор станції вводить оператор вільним текстом у майстрі.
    Без html.escape() '<'/'>' ламають парсинг HTML (Telegram узагалі не
    надсилає повідомлення), а довільний тег міг би потрапити в публічну
    видачу водіям сирим.
    """
    malicious = {
        **OPERATOR_STATION,
        "name": "Готель <script>&Едем",
        "connector_type": "Type 2 <b>hack</b>",
    }

    text = user_handlers._format_operator_station_card(1, malicious)

    assert "<script>" not in text
    assert "<b>hack</b>" not in text
    assert "&lt;script&gt;" in text
    assert "&amp;Едем" in text
    assert "Type 2 &lt;b&gt;hack&lt;/b&gt;" in text


def test_operator_station_card_neutralizes_link_injection_in_name():
    """Тег <a href=...> у назві не повинен потрапити в HTML сирим — водій не має бачити чужого посилання."""
    malicious = {**OPERATOR_STATION, "name": '<a href="https://evil.example">Клікни тут</a>'}

    text = user_handlers._format_operator_station_card(1, malicious)

    assert "<a href" not in text
    assert "&lt;a href=&quot;https://evil.example&quot;&gt;" in text


def test_ocm_station_card_without_badge_when_nothing_to_classify_from():
    unknown = {**OCM_STATION, "power_kw": None, "connector_type": None}
    text = user_handlers._format_ocm_station_card(1, unknown)
    assert text.startswith("⚡ **Станція #1**")  # без бейджа спереду


# ---------------------------------------------------------------------------
# TomTom — інфошар (бандл feature/tomtom-live-layer)
# ---------------------------------------------------------------------------

TOMTOM_STATION = {
    "id": "TOMTOM-1", "name": "АЗС Львів-Захід", "address": "Стрийська, 100",
    "lat": 49.80, "lon": 23.90, "distance_km": 2.1,
    "power_kw": 150, "connector_type": "CCS (Type 2)",
    "connectors": [{"connector_type": "CCS (Type 2)", "power_kw": 150}],
    "charging_availability_id": "avail-1",
}


def test_tomtom_station_card_includes_attribution():
    text = user_handlers._format_tomtom_station_card(1, TOMTOM_STATION)
    assert "© TomTom" in text


def test_tomtom_station_card_includes_badge_distance_power_connector():
    text = user_handlers._format_tomtom_station_card(1, TOMTOM_STATION)
    assert "⚡ Швидка (DC)" in text  # 150 кВт
    assert "АЗС Львів-Захід" in text
    assert "2.10 км" in text
    assert "150 кВт" in text
    assert "CCS (Type 2)" in text


def test_tomtom_station_card_shows_status_line_when_availability_present():
    station = {**TOMTOM_STATION, "availability": [
        {"connector_type": "CCS (Type 2)", "available": 1, "occupied": 0, "out_of_service": 0},
    ]}
    text = user_handlers._format_tomtom_station_card(1, station)
    assert "Вільно зараз" in text
    assert "1/1" in text


def test_tomtom_station_card_omits_status_line_without_availability():
    station = {**TOMTOM_STATION, "availability": None}
    text = user_handlers._format_tomtom_station_card(1, station)
    assert "Вільно зараз" not in text


def test_tomtom_station_card_escapes_html_in_name_and_address():
    malicious = {**TOMTOM_STATION, "name": "<script>x</script>", "address": "<b>адр</b>"}
    text = user_handlers._format_tomtom_station_card(1, malicious)
    assert "<script>" not in text
    assert "&lt;script&gt;" in text


# ---------------------------------------------------------------------------
# _merge_search_results з TomTom-джерелом
# ---------------------------------------------------------------------------

def test_merge_includes_tomtom_source():
    result = user_handlers._merge_search_results([], [], [TOMTOM_STATION])
    assert len(result) == 1
    assert result[0]["source"] == "tomtom"
    assert result[0]["station"] is TOMTOM_STATION


def test_merge_operator_stations_stay_first_among_equal_distance_regression():
    """Регресія: додавання TomTom-джерела не повинно зсунути операторські
    станції нижче TomTom/OCM за РІВНОЇ відстані — сортування стабільне, і
    операторський шар дії будується в списку першим."""
    operator = {**OPERATOR_STATION, "distance_km": 2.0}
    tomtom = {**TOMTOM_STATION, "distance_km": 2.0}
    ocm = {**OCM_STATION, "distance": 2.0}

    result = user_handlers._merge_search_results([ocm], [operator], [tomtom])

    assert [item["source"] for item in result] == ["operator", "tomtom", "ocm"]


def test_merge_sorts_all_three_sources_by_distance():
    near_tomtom = {**TOMTOM_STATION, "distance_km": 0.3}
    far_operator = {**OPERATOR_STATION, "distance_km": 5.0}
    mid_ocm = {**OCM_STATION, "distance": 1.5}

    result = user_handlers._merge_search_results([mid_ocm], [far_operator], [near_tomtom])

    assert [item["source"] for item in result] == ["tomtom", "ocm", "operator"]


# ---------------------------------------------------------------------------
# handle_location — маршрутизація TomTom/OCM (без Telegram/БД, лише мокнуті
# сервіси; той самий підхід прямого виклику хендлера, що в
# test_ocpp_admin_handlers.py)
# ---------------------------------------------------------------------------

class _FakeLocation:
    def __init__(self, lat, lon):
        self.latitude = lat
        self.longitude = lon


class _FakeMessage:
    def __init__(self, lat, lon):
        self.location = _FakeLocation(lat, lon)
        self.sent = []

    async def answer(self, text, **kwargs):
        self.sent.append((text, kwargs))
        return self


class _FakeState:
    def __init__(self):
        self.state = None

    async def set_state(self, state):
        self.state = state

    async def clear(self):
        self.state = None


async def test_handle_location_uses_tomtom_and_skips_ocm_when_tomtom_available(monkeypatch):
    ocm_called = []

    async def fake_ocm(*a, **kw):
        ocm_called.append((a, kw))
        return [OCM_STATION]

    async def fake_tomtom_search(*a, **kw):
        return [dict(TOMTOM_STATION)]

    availability_calls = []

    async def fake_get_availability(availability_id):
        availability_calls.append(availability_id)
        return None

    async def fake_operator_stations(*a, **kw):
        return []

    monkeypatch.setattr(user_handlers, "find_three_nearest_stations", fake_ocm)
    monkeypatch.setattr(user_handlers.tomtom_service, "search_stations_near", fake_tomtom_search)
    monkeypatch.setattr(user_handlers.tomtom_service, "get_availability", fake_get_availability)
    monkeypatch.setattr(user_handlers.op_repo, "list_public_stations_near", fake_operator_stations)

    message = _FakeMessage(49.79, 23.95)
    state = _FakeState()

    await user_handlers.handle_location(message, state)

    assert ocm_called == []  # TomTom доступний -> OCM не викликається взагалі
    assert availability_calls == ["avail-1"]  # рівно один виклик, на показану станцію
    sent_texts = [text for text, _ in message.sent]
    assert any("© TomTom" in text for text in sent_texts)


async def test_handle_location_falls_back_to_ocm_when_tomtom_unavailable(monkeypatch):
    async def fake_ocm(*a, **kw):
        return [OCM_STATION]

    async def fake_tomtom_search(*a, **kw):
        return None  # вимкнено / квота / помилка

    async def fake_operator_stations(*a, **kw):
        return []

    monkeypatch.setattr(user_handlers, "find_three_nearest_stations", fake_ocm)
    monkeypatch.setattr(user_handlers.tomtom_service, "search_stations_near", fake_tomtom_search)
    monkeypatch.setattr(user_handlers.op_repo, "list_public_stations_near", fake_operator_stations)

    message = _FakeMessage(49.79, 23.95)
    state = _FakeState()

    await user_handlers.handle_location(message, state)

    sent_texts = [text for text, _ in message.sent]
    assert any("Зубра HyperCharger" in text for text in sent_texts)  # OCM-картка дійшла
    assert state.state == user_handlers.BotStates.waiting_for_station_id


async def test_handle_location_tomtom_empty_result_falls_back_to_ocm(monkeypatch):
    """ПРАВКА 3 (рев'ю Opus 31.07.2026): успішна, але порожня відповідь
    TomTom теж фолбечить на OCM — покриття TomTom поза Львовом (де
    перевірявся ключ) не виміряне, тож [] не гарантує "станцій справді
    немає"; без фолбеку водій бачив би "не знайдено" там, де OCM щось має."""
    ocm_called = []

    async def fake_ocm(*a, **kw):
        ocm_called.append(1)
        return [OCM_STATION]

    async def fake_tomtom_search(*a, **kw):
        return []

    async def fake_operator_stations(*a, **kw):
        return []

    monkeypatch.setattr(user_handlers, "find_three_nearest_stations", fake_ocm)
    monkeypatch.setattr(user_handlers.tomtom_service, "search_stations_near", fake_tomtom_search)
    monkeypatch.setattr(user_handlers.op_repo, "list_public_stations_near", fake_operator_stations)

    message = _FakeMessage(49.79, 23.95)
    state = _FakeState()

    await user_handlers.handle_location(message, state)

    assert ocm_called == [1]
    sent_texts = [text for text, _ in message.sent]
    assert any("Зубра HyperCharger" in text for text in sent_texts)


async def test_handle_location_operator_stations_appear_first_when_closest(monkeypatch):
    """Регресія: операторський шар дії лишається у видачі й коректно
    сортується разом із новим TomTom-джерелом."""
    async def fake_ocm(*a, **kw):
        return []

    async def fake_tomtom_search(*a, **kw):
        return [{**TOMTOM_STATION, "distance_km": 5.0, "charging_availability_id": None}]

    async def fake_operator_stations(*a, **kw):
        return [{**OPERATOR_STATION, "distance_km": 0.5}]

    monkeypatch.setattr(user_handlers, "find_three_nearest_stations", fake_ocm)
    monkeypatch.setattr(user_handlers.tomtom_service, "search_stations_near", fake_tomtom_search)
    monkeypatch.setattr(user_handlers.op_repo, "list_public_stations_near", fake_operator_stations)

    message = _FakeMessage(49.79, 23.95)
    state = _FakeState()

    await user_handlers.handle_location(message, state)

    sent_texts = [text for text, _ in message.sent]
    # Друге повідомлення (після "Шукаємо..."/"Знайдено N") — перша картка станції.
    first_card = sent_texts[2]
    assert "Готель Едем" in first_card
