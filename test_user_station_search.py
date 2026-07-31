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
# _dedupe_by_proximity / _dedupe_winner (фікс-бандл після живого смоуку
# 31.07.2026: TomTom і OCM обидва індексують публічні станції — та сама
# фізична точка на карті могла прийти двічі, коли джерела почали
# об'єднуватись, а не заміщувати одне одного)
# ---------------------------------------------------------------------------

_BASE_LAT, _BASE_LON = 49.8000, 23.9000
_NEAR_OFFSET_DEG = 0.0005   # ~55 м (широта) — усередині DEDUPE_DISTANCE_KM
_FAR_OFFSET_DEG = 0.003     # ~334 м — за межею DEDUPE_DISTANCE_KM


def test_dedupe_collapses_near_duplicate_from_different_sources():
    ocm = {**OCM_STATION, "lat": _BASE_LAT, "lon": _BASE_LON, "distance": 1.0}
    tomtom = {**TOMTOM_STATION, "lat": _BASE_LAT + _NEAR_OFFSET_DEG, "lon": _BASE_LON, "distance_km": 1.0}

    merged = user_handlers._merge_search_results([ocm], [], [tomtom])
    result = user_handlers._dedupe_by_proximity(merged)

    assert len(result) == 1


def test_dedupe_keeps_both_stations_beyond_100m():
    ocm = {**OCM_STATION, "lat": _BASE_LAT, "lon": _BASE_LON, "distance": 1.0}
    tomtom = {**TOMTOM_STATION, "lat": _BASE_LAT + _FAR_OFFSET_DEG, "lon": _BASE_LON, "distance_km": 1.0}

    merged = user_handlers._merge_search_results([ocm], [], [tomtom])
    result = user_handlers._dedupe_by_proximity(merged)

    assert len(result) == 2


def test_dedupe_winner_operator_beats_both_ocm_and_tomtom():
    operator = {**OPERATOR_STATION, "lat": _BASE_LAT, "lng": _BASE_LON, "distance_km": 1.0}
    ocm = {**OCM_STATION, "lat": _BASE_LAT, "lon": _BASE_LON, "distance": 1.0}
    tomtom = {**TOMTOM_STATION, "lat": _BASE_LAT, "lon": _BASE_LON, "distance_km": 1.0}

    merged = user_handlers._merge_search_results([ocm], [operator], [tomtom])
    result = user_handlers._dedupe_by_proximity(merged)

    assert len(result) == 1
    assert result[0]["source"] == "operator"


def test_dedupe_winner_recognizes_operator_lng_key():
    """Регресія: операторська станція з БД несе довготу під ключем `lng`
    (app/database/operators_repo.py), а не `lon`, як OCM/TomTom. Без цього
    _station_coords() ніколи не бачить координат оператора — дедуп мовчки
    пропускає операторський дублікат замість того, щоб лишити саме його."""
    operator = {**OPERATOR_STATION, "lat": _BASE_LAT, "lng": _BASE_LON, "distance_km": 1.0}
    tomtom = {**TOMTOM_STATION, "lat": _BASE_LAT, "lon": _BASE_LON, "distance_km": 1.0}

    merged = user_handlers._merge_search_results([], [operator], [tomtom])
    result = user_handlers._dedupe_by_proximity(merged)

    assert len(result) == 1
    assert result[0]["source"] == "operator"


def test_dedupe_winner_prefers_station_with_more_metadata():
    """Між OCM і TomTom (без операторської) перемагає та, у якої БІЛЬШЕ
    метаданих (потужність/тип конектора) — навіть якщо це не TomTom."""
    metadata_rich_ocm = {
        **OCM_STATION, "lat": _BASE_LAT, "lon": _BASE_LON, "distance": 1.0,
        "power_kw": 240, "connector_type": "CCS (Type 2)",
    }
    metadata_poor_tomtom = {
        **TOMTOM_STATION, "lat": _BASE_LAT, "lon": _BASE_LON, "distance_km": 1.0,
        "power_kw": None, "connector_type": None,
    }

    merged = user_handlers._merge_search_results([metadata_rich_ocm], [], [metadata_poor_tomtom])
    result = user_handlers._dedupe_by_proximity(merged)

    assert len(result) == 1
    assert result[0]["source"] == "ocm"


def test_dedupe_winner_prefers_tomtom_on_equal_metadata():
    ocm = {
        **OCM_STATION, "lat": _BASE_LAT, "lon": _BASE_LON, "distance": 1.0,
        "power_kw": 150, "connector_type": "CCS (Type 2)",
    }
    tomtom = {
        **TOMTOM_STATION, "lat": _BASE_LAT, "lon": _BASE_LON, "distance_km": 1.0,
        "power_kw": 150, "connector_type": "CCS (Type 2)",
    }

    merged = user_handlers._merge_search_results([ocm], [], [tomtom])
    result = user_handlers._dedupe_by_proximity(merged)

    assert len(result) == 1
    assert result[0]["source"] == "tomtom"


def test_dedupe_does_not_compare_stations_from_the_same_source():
    ocm_a = {**OCM_STATION, "id": "OCM-1", "lat": _BASE_LAT, "lon": _BASE_LON, "distance": 1.0}
    ocm_b = {**OCM_STATION, "id": "OCM-2", "lat": _BASE_LAT, "lon": _BASE_LON, "distance": 1.1}

    merged = user_handlers._merge_search_results([ocm_a, ocm_b], [], [])
    result = user_handlers._dedupe_by_proximity(merged)

    assert len(result) == 2


# ---------------------------------------------------------------------------
# _is_dc_station (бандл feature/search-results-single-message, ПРАВКА 1) —
# окремий предикат для ВІДБОРУ квоти, не для бейджа картки —
# classify_station_speed лишається недоторканою.
# ---------------------------------------------------------------------------

def test_is_dc_station_true_for_tomtom_current_type_dc():
    station = {
        "power_kw": None, "connector_type": None,
        "connectors": [{"connector_type": "Type 2", "power_kw": None, "current_type": "DC"}],
    }
    assert user_handlers._is_dc_station(station) is True


def test_is_dc_station_true_for_high_power():
    station = {"power_kw": 50, "connector_type": None, "connectors": None}
    assert user_handlers._is_dc_station(station) is True


def test_is_dc_station_false_for_power_just_below_threshold():
    station = {"power_kw": 49.9, "connector_type": None, "connectors": None}
    assert user_handlers._is_dc_station(station) is False


def test_is_dc_station_true_for_connector_hint():
    for hint_text in ("CCS (Type 2)", "CHAdeMO", "GB/T DC"):
        station = {"power_kw": None, "connector_type": hint_text, "connectors": None}
        assert user_handlers._is_dc_station(station) is True, hint_text


def test_is_dc_station_false_without_any_signal():
    """Станція без потужності, конектора чи TomTom-конекторів — AC за
    замовчуванням, не вгадуємо "швидка"."""
    station = {"power_kw": None, "connector_type": "Type 2", "connectors": None}
    assert user_handlers._is_dc_station(station) is False


def test_is_dc_station_ignores_non_list_connectors_field():
    """OCM-станції несуть 'connectors' як РЯДОК ("CCS (240 кВт) x2"), не
    список словників, як TomTom — без isinstance-перевірки рядок
    ітерувався б по символах, і .get() на символі впав би AttributeError."""
    station = {"power_kw": None, "connector_type": None, "connectors": "CCS (240 кВт) x2"}
    assert user_handlers._is_dc_station(station) is False


# ---------------------------------------------------------------------------
# _select_by_quota (ПРАВКА 2) — відбір оператор -> DC -> AC замість зрізу
# [:N] за відстанню.
# ---------------------------------------------------------------------------

def _quota_item(source, distance_km, is_dc):
    """Мінімальний елемент _merge_search_results для тестів квоти — is_dc
    виражається напряму через power_kw (>=50 -> DC), щоб не змішувати
    перевірку квоти з перевіркою самого _is_dc_station (та вже перевірена
    окремо вище своїми тестами)."""
    power_kw = 100 if is_dc else 10
    return {
        "source": source, "distance_km": distance_km,
        "station": {"power_kw": power_kw, "connector_type": None, "connectors": None},
    }


def test_select_by_quota_6dc_6ac_picks_4dc_2ac():
    items = (
        [_quota_item("ocm", i, True) for i in range(1, 7)]      # 6 DC
        + [_quota_item("ocm", i, False) for i in range(7, 13)]  # 6 AC
    )
    result = user_handlers._select_by_quota(items)

    dc_count = sum(1 for it in result if user_handlers._is_dc_station(it["station"]))
    assert dc_count == 4
    assert len(result) - dc_count == 2
    assert len(result) == 6


def test_select_by_quota_backfills_ac_when_dc_scarce():
    items = (
        [_quota_item("ocm", 1, True), _quota_item("ocm", 2, True)]  # лише 2 DC
        + [_quota_item("ocm", i, False) for i in range(3, 9)]       # 6 AC
    )
    result = user_handlers._select_by_quota(items)

    dc_count = sum(1 for it in result if user_handlers._is_dc_station(it["station"]))
    assert dc_count == 2
    assert len(result) - dc_count == 4
    assert len(result) == 6


def test_select_by_quota_ac_only_fills_all_six():
    items = [_quota_item("ocm", i, False) for i in range(1, 8)]  # 7 AC, 0 DC
    result = user_handlers._select_by_quota(items)

    assert len(result) == 6
    assert all(not user_handlers._is_dc_station(it["station"]) for it in result)


def test_select_by_quota_backfills_dc_when_ac_scarce():
    """Симетричний випадок ("і навпаки" зі специфікації правки): AC не
    вистачає заповнити квоту -> решту добирають DC понад MAX_DC_SHOWN."""
    items = (
        [_quota_item("ocm", i, True) for i in range(1, 8)]  # 7 DC
        + [_quota_item("ocm", 8, False)]                     # лише 1 AC
    )
    result = user_handlers._select_by_quota(items)

    dc_count = sum(1 for it in result if user_handlers._is_dc_station(it["station"]))
    assert len(result) == 6
    assert len(result) - dc_count == 1
    assert dc_count == 5


def test_select_by_quota_operator_first_capped_at_two():
    items = (
        [_quota_item("operator", i, True) for i in range(1, 4)]  # 3 оператори в пулі
        + [_quota_item("ocm", i, True) for i in range(4, 10)]
    )
    result = user_handlers._select_by_quota(items)

    operators = [it for it in result if it["source"] == "operator"]
    assert len(operators) == 2
    assert [it["source"] for it in result[:2]] == ["operator", "operator"]


def test_select_by_quota_ceiling_is_six_total():
    items = (
        [_quota_item("operator", 0.1, True), _quota_item("operator", 0.2, True)]
        + [_quota_item("ocm", i, True) for i in range(1, 10)]
        + [_quota_item("tomtom", i, False) for i in range(10, 20)]
    )
    result = user_handlers._select_by_quota(items)

    assert len(result) == user_handlers.MAX_STATIONS_TOTAL


# ---------------------------------------------------------------------------
# ОДНЕ повідомлення видачі — _format_search_results_message (ПРАВКА 4)
# ---------------------------------------------------------------------------

def test_format_search_results_message_includes_results_count_header():
    """Рев'ю: лічильник, що раніше був окремим повідомленням ("🎯 Знайдено
    N станцій поруч:"), тепер перший рядок цього ЖЕ повідомлення."""
    combined = user_handlers._merge_search_results(
        [{**OCM_STATION, "lat": _BASE_LAT, "lon": _BASE_LON, "distance": 1.0}], [], [],
    )
    text = user_handlers._format_search_results_message(combined)

    assert text.startswith("🎯 **Знайдено 1 станцій поруч:**")


def test_format_search_results_message_numbers_entries_and_keeps_ocm_id():
    combined = user_handlers._merge_search_results(
        [{**OCM_STATION, "lat": _BASE_LAT, "lon": _BASE_LON, "distance": 1.0}], [], [],
    )
    text = user_handlers._format_search_results_message(combined)

    assert "1. " in text
    assert "OCM-999" in text  # флоу "надішліть ID" не чіпається цим бандлом


def test_format_search_results_message_escapes_html_in_operator_name():
    malicious = {
        **OPERATOR_STATION, "name": "Готель <script>x</script>",
        "lat": _BASE_LAT, "lng": _BASE_LON, "distance_km": 1.0,
    }
    combined = [{"source": "operator", "distance_km": 1.0, "station": malicious}]

    text = user_handlers._format_search_results_message(combined)

    assert "<script>" not in text
    assert "&lt;script&gt;" in text


def test_attribution_footer_present_when_tomtom_in_results():
    combined = [{"source": "tomtom", "distance_km": 1.0, "station": TOMTOM_STATION}]
    text = user_handlers._format_search_results_message(combined)
    assert "© TomTom" in text


def test_attribution_footer_present_when_ocm_in_results():
    combined = user_handlers._merge_search_results([OCM_STATION], [], [])
    text = user_handlers._format_search_results_message(combined)
    assert user_handlers.OCM_ATTRIBUTION_TEXT in text


def test_attribution_footer_absent_for_operator_only_results():
    """Операторська видача — власна, без зовнішньої ліцензії, тому підпис
    не потрібен узагалі (ні TomTom, ні OCM)."""
    operator = {**OPERATOR_STATION, "lat": _BASE_LAT, "lng": _BASE_LON}
    combined = user_handlers._merge_search_results([], [operator], [])
    text = user_handlers._format_search_results_message(combined)
    assert "© TomTom" not in text
    assert user_handlers.OCM_ATTRIBUTION_TEXT not in text


def test_truncate_shortens_long_text_with_ellipsis():
    result = user_handlers._truncate("А" * 100, max_len=10)
    assert len(result) == 10
    assert result.endswith("…")


def test_truncate_keeps_short_text_unchanged():
    assert user_handlers._truncate("Готель Едем", max_len=60) == "Готель Едем"


def test_build_keyboard_items_sets_pay_url_for_operator_only():
    combined = [
        {"source": "operator", "distance_km": 0.5,
         "station": {**OPERATOR_STATION, "lat": _BASE_LAT, "lng": _BASE_LON}},
        {"source": "tomtom", "distance_km": 1.0,
         "station": {**TOMTOM_STATION, "lat": _BASE_LAT, "lon": _BASE_LON}},
    ]
    items = user_handlers._build_keyboard_items(combined)

    assert items[0]["pay_url"] == f"{user_handlers.PUBLIC_BASE_URL}/s/abc123"
    assert items[1]["pay_url"] is None


# ---------------------------------------------------------------------------
# get_search_results_keyboard (app/keyboards/reply.py, ПРАВКА 5)
# ---------------------------------------------------------------------------

from app.keyboards.reply import get_search_results_keyboard  # noqa: E402


def test_search_results_keyboard_has_one_row_per_station_with_own_coords():
    items = [
        {"idx": 1, "lat": 49.80, "lon": 23.90, "pay_url": None},
        {"idx": 2, "lat": 49.81, "lon": 23.91, "pay_url": None},
    ]
    markup = get_search_results_keyboard(items)

    assert len(markup.inline_keyboard) == 2
    row1, row2 = markup.inline_keyboard
    assert "q=49.8,23.9" in row1[0].url
    assert "ll=49.8,23.9" in row1[1].url
    assert "q=49.81,23.91" in row2[0].url
    assert "ll=49.81,23.91" in row2[1].url


def test_search_results_keyboard_operator_row_has_pay_button():
    items = [{"idx": 1, "lat": 49.80, "lon": 23.90, "pay_url": "https://evolt.ua/s/abc123"}]
    markup = get_search_results_keyboard(items)

    row = markup.inline_keyboard[0]
    assert len(row) == 3
    assert row[2].url == "https://evolt.ua/s/abc123"
    assert "Оплатити" in row[2].text


def test_search_results_keyboard_non_operator_row_has_two_buttons_only():
    items = [{"idx": 1, "lat": 49.80, "lon": 23.90, "pay_url": None}]
    markup = get_search_results_keyboard(items)
    assert len(markup.inline_keyboard[0]) == 2


# ---------------------------------------------------------------------------
# handle_location — злиття OCM+TomTom+оператор, дедуп, ліміт (без Telegram/
# БД, лише мокнуті сервіси; той самий підхід прямого виклику хендлера, що в
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


def _far_station(template, source_key, idx, km_offset):
    """Станція на km_offset* _FAR_OFFSET_DEG*~370 км-масштабі від _BASE_LAT —
    заздалегідь далеко за DEDUPE_DISTANCE_KM від будь-якої іншої такої ж
    станції, щоб тести ліміту не випадково зачепили дедуп."""
    lat = _BASE_LAT + km_offset * 0.01
    station = {**template, "id": f"{source_key}-{idx}", "lat": lat, "lng": lat, "lon": lat}
    if source_key == "ocm":
        station["distance"] = km_offset
    else:
        station["distance_km"] = km_offset
    return station


async def test_handle_location_queries_ocm_even_when_tomtom_returns_results(monkeypatch):
    """Регресія на саму причину бандла (живий смоук 31.07.2026): раніше
    успішна відповідь TomTom вимикала запит до OCM повністю — швидкі
    DC-станції, знані лише OCM, зникали з видачі. OCM тепер запитується
    ЗАВЖДИ, паралельно з TomTom."""
    ocm_called = []

    async def fake_ocm(*a, **kw):
        ocm_called.append(1)
        return [{**OCM_STATION, "lat": _BASE_LAT + 1.0, "lon": _BASE_LON, "distance": 50.0}]

    async def fake_tomtom_search(*a, **kw):
        return [{**TOMTOM_STATION, "lat": _BASE_LAT, "lon": _BASE_LON, "distance_km": 1.0}]

    async def fake_get_availability(availability_id):
        return None

    async def fake_operator_stations(*a, **kw):
        return []

    monkeypatch.setattr(user_handlers, "find_three_nearest_stations", fake_ocm)
    monkeypatch.setattr(user_handlers.tomtom_service, "search_stations_near", fake_tomtom_search)
    monkeypatch.setattr(user_handlers.tomtom_service, "get_availability", fake_get_availability)
    monkeypatch.setattr(user_handlers.op_repo, "list_public_stations_near", fake_operator_stations)

    message = _FakeMessage(_BASE_LAT, _BASE_LON)
    state = _FakeState()

    await user_handlers.handle_location(message, state)

    assert ocm_called == [1]
    sent_texts = [text for text, _ in message.sent]
    assert any("Зубра HyperCharger" in text for text in sent_texts)  # OCM-картка дійшла
    assert any("© TomTom" in text for text in sent_texts)  # і TomTom-картка теж


async def test_handle_location_dedupes_near_duplicate_across_sources(monkeypatch):
    """Дедуп перевіряється через ВМІСТ повідомлення — вмикаємо
    SEARCH_SINGLE_MESSAGE явно, бо дефолт (окремі картки, бандл
    feature/adaptive-radius-and-cards) не дає одного тексту для перевірки."""
    monkeypatch.setattr(user_handlers, "SEARCH_SINGLE_MESSAGE", True)

    async def fake_ocm(*a, **kw):
        return [{**OCM_STATION, "lat": _BASE_LAT, "lon": _BASE_LON, "distance": 1.0}]

    async def fake_tomtom_search(*a, **kw):
        return [{**TOMTOM_STATION, "lat": _BASE_LAT + _NEAR_OFFSET_DEG, "lon": _BASE_LON, "distance_km": 1.0}]

    async def fake_get_availability(availability_id):
        return None

    async def fake_operator_stations(*a, **kw):
        return []

    monkeypatch.setattr(user_handlers, "find_three_nearest_stations", fake_ocm)
    monkeypatch.setattr(user_handlers.tomtom_service, "search_stations_near", fake_tomtom_search)
    monkeypatch.setattr(user_handlers.tomtom_service, "get_availability", fake_get_availability)
    monkeypatch.setattr(user_handlers.op_repo, "list_public_stations_near", fake_operator_stations)

    message = _FakeMessage(_BASE_LAT, _BASE_LON)
    state = _FakeState()

    await user_handlers.handle_location(message, state)

    # sent[0] — "Шукаємо...", sent[1] — ОДНЕ повідомлення з видачею
    # (SEARCH_SINGLE_MESSAGE, дефолт): дублікат схлопнувся в один запис.
    text = message.sent[1][0]
    assert "1. " in text
    assert "2. " not in text


async def test_handle_location_quota_caps_total_at_six(monkeypatch):
    """Стеля видачі — MAX_STATIONS_TOTAL=6 через _select_by_quota, не
    простий зріз за відстанню. Усі 7 кандидатів тут DC (успадковують
    power_kw>=50 з шаблонів OCM_STATION/TOMTOM_STATION) — перевіряє саме
    стелю; сам розподіл DC/AC перевірений окремо юніт-тестами
    _select_by_quota вище. SEARCH_SINGLE_MESSAGE увімкнено явно — вміст
    перевіряється в ОДНОМУ повідомленні, дефолт тепер окремі картки."""
    monkeypatch.setattr(user_handlers, "SEARCH_SINGLE_MESSAGE", True)

    async def fake_ocm(*a, **kw):
        return [_far_station(OCM_STATION, "ocm", i, i) for i in range(1, 4)]  # 1,2,3 км

    async def fake_tomtom_search(*a, **kw):
        return [_far_station(TOMTOM_STATION, "tomtom", i, i) for i in range(4, 8)]  # 4,5,6,7 км

    async def fake_get_availability(availability_id):
        return None

    async def fake_operator_stations(*a, **kw):
        return []

    monkeypatch.setattr(user_handlers, "find_three_nearest_stations", fake_ocm)
    monkeypatch.setattr(user_handlers.tomtom_service, "search_stations_near", fake_tomtom_search)
    monkeypatch.setattr(user_handlers.tomtom_service, "get_availability", fake_get_availability)
    monkeypatch.setattr(user_handlers.op_repo, "list_public_stations_near", fake_operator_stations)

    message = _FakeMessage(_BASE_LAT, _BASE_LON)
    state = _FakeState()

    await user_handlers.handle_location(message, state)

    text = message.sent[1][0]
    assert "6. " in text
    assert "7. " not in text


async def test_handle_location_fetches_availability_only_for_shown_tomtom_stations(monkeypatch):
    """TOMTOM_SEARCH_LIMIT=10 навмисно ширший за MAX_STATIONS_TOTAL=6, щоб
    після злиття/дедупу/квоти було з чого обирати — але live-статус має
    тягнутись ЛИШЕ для станцій, що реально потрапили у фінальну видачу, не
    для всіх 7 сирих результатів nearbySearch."""
    async def fake_ocm(*a, **kw):
        return []

    async def fake_tomtom_search(*a, **kw):
        return [_far_station(TOMTOM_STATION, "tomtom", i, i) for i in range(1, 8)]  # 7 станцій

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

    message = _FakeMessage(_BASE_LAT, _BASE_LON)
    state = _FakeState()

    await user_handlers.handle_location(message, state)

    assert len(availability_calls) == user_handlers.MAX_STATIONS_TOTAL


async def test_handle_location_operator_stations_appear_first_when_closest(monkeypatch):
    """Регресія: операторський шар дії лишається у видачі й коректно
    сортується разом із TomTom-джерелом. SEARCH_SINGLE_MESSAGE увімкнено
    явно — порядок перевіряється всередині ОДНОГО тексту, дефолт тепер
    окремі картки."""
    monkeypatch.setattr(user_handlers, "SEARCH_SINGLE_MESSAGE", True)

    async def fake_ocm(*a, **kw):
        return []

    async def fake_tomtom_search(*a, **kw):
        return [{**TOMTOM_STATION, "lat": _BASE_LAT + 5.0, "lon": _BASE_LON, "distance_km": 5.0}]

    async def fake_operator_stations(*a, **kw):
        return [{**OPERATOR_STATION, "lat": _BASE_LAT, "lng": _BASE_LON, "distance_km": 0.5}]

    monkeypatch.setattr(user_handlers, "find_three_nearest_stations", fake_ocm)
    monkeypatch.setattr(user_handlers.tomtom_service, "search_stations_near", fake_tomtom_search)
    monkeypatch.setattr(user_handlers.op_repo, "list_public_stations_near", fake_operator_stations)

    message = _FakeMessage(_BASE_LAT, _BASE_LON)
    state = _FakeState()

    await user_handlers.handle_location(message, state)

    # sent[1] — ОДНЕ повідомлення з видачею; text.split("\n\n")[0] — рядок-
    # лічильник "🎯 Знайдено N...", [1] — перший запис. Він має бути
    # операторським, незалежно від того, що TomTom формально ближчий за
    # _select_by_quota — операторський блок завжди перший.
    text = message.sent[1][0]
    first_entry = text.split("\n\n")[1]
    assert "Готель Едем" in first_entry


# ---------------------------------------------------------------------------
# ПРАВКА 7 — SEARCH_SINGLE_MESSAGE (прапорець-відкат без деплою)
# ---------------------------------------------------------------------------

async def test_handle_location_sends_n_separate_messages_by_default(monkeypatch):
    """Бандл feature/adaptive-radius-and-cards: дефолт ЗМІНЕНО на окремі
    картки (рішення власника 31.07.2026) — без змінної SEARCH_SINGLE_MESSAGE
    в оточенні (як у цьому тестовому середовищі) видача йде старим
    рендером. Прапорець НЕ монкіпатчиться навмисно — перевіряє реальний
    дефолт, обчислений при імпорті модуля, а не підмінене значення."""
    async def fake_ocm(*a, **kw):
        return [{**OCM_STATION, "lat": _BASE_LAT, "lon": _BASE_LON, "distance": 1.0}]

    async def fake_tomtom_search(*a, **kw):
        return [{**TOMTOM_STATION, "lat": _BASE_LAT + 1.0, "lon": _BASE_LON, "distance_km": 5.0}]

    async def fake_get_availability(availability_id):
        return None

    async def fake_operator_stations(*a, **kw):
        return []

    monkeypatch.setattr(user_handlers, "find_three_nearest_stations", fake_ocm)
    monkeypatch.setattr(user_handlers.tomtom_service, "search_stations_near", fake_tomtom_search)
    monkeypatch.setattr(user_handlers.tomtom_service, "get_availability", fake_get_availability)
    monkeypatch.setattr(user_handlers.op_repo, "list_public_stations_near", fake_operator_stations)

    message = _FakeMessage(_BASE_LAT, _BASE_LON)
    state = _FakeState()

    await user_handlers.handle_location(message, state)

    # "Шукаємо..." + "Знайдено 2 станцій..." + картка OCM + картка TomTom.
    assert len(message.sent) == 4
    sent_texts = [text for text, _ in message.sent]
    assert sent_texts[1] == "🎯 **Знайдено 2 станцій поруч:**"
    assert any("Зубра HyperCharger" in text for text in sent_texts)
    assert any("© TomTom" in text for text in sent_texts)


async def test_handle_location_sends_exactly_one_message_when_flag_enabled(monkeypatch):
    """SEARCH_SINGLE_MESSAGE=true (явно ввімкнено) — одноповідомленнєвий
    рендер лишається доступним за прапорцем (не видалений цим бандлом)."""
    monkeypatch.setattr(user_handlers, "SEARCH_SINGLE_MESSAGE", True)

    async def fake_ocm(*a, **kw):
        return [{**OCM_STATION, "lat": _BASE_LAT, "lon": _BASE_LON, "distance": 1.0}]

    async def fake_tomtom_search(*a, **kw):
        return [{**TOMTOM_STATION, "lat": _BASE_LAT + 1.0, "lon": _BASE_LON, "distance_km": 5.0}]

    async def fake_get_availability(availability_id):
        return None

    async def fake_operator_stations(*a, **kw):
        return [{**OPERATOR_STATION, "lat": _BASE_LAT, "lng": _BASE_LON + 1.0, "distance_km": 0.3}]

    monkeypatch.setattr(user_handlers, "find_three_nearest_stations", fake_ocm)
    monkeypatch.setattr(user_handlers.tomtom_service, "search_stations_near", fake_tomtom_search)
    monkeypatch.setattr(user_handlers.tomtom_service, "get_availability", fake_get_availability)
    monkeypatch.setattr(user_handlers.op_repo, "list_public_stations_near", fake_operator_stations)

    message = _FakeMessage(_BASE_LAT, _BASE_LON)
    state = _FakeState()

    await user_handlers.handle_location(message, state)

    assert len(message.sent) == 2  # "🔍 Шукаємо..." + одне повідомлення з видачею


# ---------------------------------------------------------------------------
# Адаптивний радіус TomTom (_search_tomtom_stations, живий смоук 31.07.2026
# за містом)
# ---------------------------------------------------------------------------

async def test_search_tomtom_stations_retries_with_fallback_radius_on_empty(monkeypatch):
    calls = []

    async def fake_search(lat, lon, radius_km, limit):
        calls.append(radius_km)
        if radius_km == user_handlers.TOMTOM_SEARCH_RADIUS_KM:
            return []
        return [dict(TOMTOM_STATION)]

    monkeypatch.setattr(user_handlers.tomtom_service, "search_stations_near", fake_search)

    result = await user_handlers._search_tomtom_stations(_BASE_LAT, _BASE_LON)

    assert calls == [user_handlers.TOMTOM_SEARCH_RADIUS_KM, user_handlers.TOMTOM_FALLBACK_RADIUS_KM]
    assert len(result) == 1


async def test_search_tomtom_stations_no_retry_when_first_call_non_empty(monkeypatch):
    calls = []

    async def fake_search(lat, lon, radius_km, limit):
        calls.append(radius_km)
        return [dict(TOMTOM_STATION)]

    monkeypatch.setattr(user_handlers.tomtom_service, "search_stations_near", fake_search)

    result = await user_handlers._search_tomtom_stations(_BASE_LAT, _BASE_LON)

    assert calls == [user_handlers.TOMTOM_SEARCH_RADIUS_KM]
    assert len(result) == 1


async def test_search_tomtom_stations_no_retry_on_none(monkeypatch):
    """None = шар недоступний (немає ключа/вичерпана квота/помилка) —
    повтору НЕМАЄ, інакше другий виклик лише подвоїть відмову й спалить
    добову квоту TomTom."""
    calls = []

    async def fake_search(lat, lon, radius_km, limit):
        calls.append(radius_km)
        return None

    monkeypatch.setattr(user_handlers.tomtom_service, "search_stations_near", fake_search)

    result = await user_handlers._search_tomtom_stations(_BASE_LAT, _BASE_LON)

    assert calls == [user_handlers.TOMTOM_SEARCH_RADIUS_KM]
    assert result == []
