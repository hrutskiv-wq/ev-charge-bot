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
