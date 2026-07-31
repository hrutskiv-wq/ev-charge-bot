"""
TomTom як живий інфошар станцій у водійському пошуку.

Юридичне обмеження (T&C TomTom п. 11.4): кешування даних для обслуговування
кількох користувачів заборонено — дозволений лише живий запит під конкретний
запит конкретного користувача. Тому в цьому модулі свідомо НЕМАЄ кешу,
НЕМАЄ запису в БД і НЕМАЄ фонового збору даних. Якщо колись захочеться
"щоб швидше" — не додавати кеш тут, а винести пункт у беклог із посиланням
на цей коментар і п. 11.4.

Без ключа (TOMTOM_API_KEY не задано) сервіс тихо вимкнений — обидві функції
повертають None без жодного HTTP-виклику; водійський пошук (`app/handlers/
user.py::handle_location`) далі показує OCM і операторські станції, TomTom
просто дає порожній шар.
"""
import os
import logging
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

TOMTOM_API_KEY = os.getenv("TOMTOM_API_KEY")

TOMTOM_CATEGORY_EV_CHARGING = 7309

# Тверда стеля акаунта TomTom — 2500 запитів/добу. Бюджет свідомо нижчий за
# стелю: баг у циклі (напр. availability без ліміту на кожну знайдену
# станцію) мусить упертися в запобіжник задовго до реальної стелі рахунку.
#
# Реальна арифметика (рев'ю Opus 31.07.2026, уточнено 31.07.2026 після
# живого смоуку, ще раз 31.07.2026 у бандлі квоти видачі, і ще раз
# 31.07.2026 у бандлі адаптивного радіуса): найгірший випадок одного
# handle_location() — це ДО 2 викликів search_stations_near() (адаптивний
# радіус, app/handlers/user.py::_search_tomtom_stations — другий лише коли
# перший повернув [] на TOMTOM_SEARCH_RADIUS_KM, ніколи не на None) + до
# MAX_STATIONS_TOTAL викликів get_availability() (по одному на кожну
# TomTom-станцію у ФІНАЛЬНІЙ видачі після злиття з OCM/оператором, дедупу
# й відбору квотою _select_by_quota; MAX_STATIONS_TOTAL=6 станом на зараз)
# = до 8 звернень за один пошук водія. Тобто бюджет 2000 — це ~250 пошуків
# водіїв на добу (2000 / 8), НЕ 2000. TOMTOM_SEARCH_LIMIT (розмір ОДНІЄЇ
# відповіді nearbySearch, піднятий до 10, щоб дедуп і квота мали з чого
# вибирати) на цю арифметику НЕ впливає — той самий один HTTP-виклик
# незалежно від значення.
TOMTOM_DAILY_BUDGET = 2000

ATTRIBUTION_TEXT = "© TomTom"

# Мапа "сирих" кодів конекторів TomTom -> той самий стиль підпису, що вже
# використовується для OCM/операторських станцій (app/services/ocm_service.py,
# classify_station_speed) — щоб бейдж швидкості й текст картки збігались.
CONNECTOR_LABELS = {
    "IEC62196Type2CCS": "CCS (Type 2)",
    "IEC62196Type1CCS": "CCS (Type 1)",
    "Chademo": "CHAdeMO",
    "IEC62196Type2Outlet": "Type 2",
    "IEC62196Type2CableAttached": "Type 2",
    "IEC62196Type1Outlet": "Type 1",
    "IEC62196Type1CableAttached": "Type 1",
    "IEC62196Type3": "Type 3",
    "Tesla": "Tesla",
    "TeslaSupercharger": "Tesla Supercharger",
    "StandardHouseholdCountrySpecific": "Schuko",
    "IEC60309AC1PhaseBlue": "CEE (1ф)",
    "IEC60309AC3PhaseRed": "CEE (3ф)",
}


class _DailyBudget:
    """Лічильник запитів у пам'яті процесу, добове вікно UTC. Навмисно не
    переживає рестарт процесу — це запобіжник від "бага в циклі за хвилини",
    а не точний білінговий облік для рахунку TomTom."""

    def __init__(self, limit):
        self._limit = limit
        self._day = None
        self._count = 0

    def try_consume(self):
        today = datetime.now(timezone.utc).date()
        if today != self._day:
            self._day = today
            self._count = 0
        if self._count >= self._limit:
            return False
        self._count += 1
        return True


_budget = _DailyBudget(TOMTOM_DAILY_BUDGET)


def is_enabled() -> bool:
    return bool(TOMTOM_API_KEY)


def _connector_label(raw_type):
    if not raw_type:
        return None
    return CONNECTOR_LABELS.get(raw_type, raw_type)


def _normalize_search_result(result):
    poi = result.get("poi") or {}
    position = result.get("position") or {}
    address = result.get("address") or {}

    lat = position.get("lat")
    lon = position.get("lon")
    if lat is None or lon is None:
        return None

    # chargingPark — поле ВЕРХНЬОГО рівня result, НЕ всередині poi (форма
    # звірена з живою відповіддю TomTom 31.07.2026: poi.chargingPark завжди
    # порожній; result.chargingPark.connectors несе реальні дані конекторів).
    charging_park = result.get("chargingPark") or {}
    connectors = []
    best_power_kw = None
    best_connector_type = None
    best_is_dc = False
    for c in charging_park.get("connectors") or []:
        label = _connector_label(c.get("connectorType"))
        power = c.get("ratedPowerKW")
        # currentType (AC1/AC3/DC) — прямий сигнал TomTom, надійніший за
        # вгадування зі списку хінтів у classify_station_speed; використовуємо
        # його лише для вибору, ЯКИЙ конектор представляє станцію, коли
        # потужність невідома. Сама classify_station_speed не чіпається —
        # потужність лишається головним критерієм там.
        current_type = c.get("currentType")
        is_dc = current_type == "DC"
        connectors.append({"connector_type": label, "power_kw": power, "current_type": current_type})

        if power and (best_power_kw is None or power > best_power_kw):
            best_power_kw = power
            best_connector_type = label
            best_is_dc = is_dc
        elif best_power_kw is None and (best_connector_type is None or (is_dc and not best_is_dc)):
            best_connector_type = label
            best_is_dc = is_dc

    dist_m = result.get("dist")
    distance_km = (dist_m / 1000) if dist_m is not None else None

    availability_id = ((result.get("dataSources") or {}).get("chargingAvailability") or {}).get("id")

    return {
        "id": f"TOMTOM-{result.get('id')}",
        "name": poi.get("name") or "Без назви",
        "lat": lat,
        "lon": lon,
        "address": address.get("freeformAddress") or "Адреса не вказана",
        "distance_km": distance_km,
        "connectors": connectors,
        "power_kw": best_power_kw,
        "connector_type": best_connector_type,
        "charging_availability_id": availability_id,
    }


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _normalize_availability(data):
    result = []
    for c in data.get("connectors") or []:
        current = ((c.get("availability") or {}).get("current")) or {}
        result.append({
            "connector_type": _connector_label(c.get("type")),
            "available": _to_int(current.get("available")),
            "occupied": _to_int(current.get("occupied")),
            "out_of_service": _to_int(current.get("outOfService")),
        })
    return result


async def search_stations_near(lat, lon, radius_km=15, limit=5):
    """
    Живий nearbySearch по EV-станціях (categorySet=7309) навколо водія.

    Повертає:
    - `None`, якщо шар недоступний ПРЯМО ЗАРАЗ (немає ключа, вичерпано
      добову квоту, мережева помилка чи не-200 від TomTom);
    - `[]`, якщо запит успішний, але станцій у радіусі не знайдено;
    - непорожній список нормалізованих станцій інакше.

    Розрізнення `None`/`[]` — лише для логування (`None` рахується як "шар
    недоступний" у логах-попередженнях вище). Окремого фолбеку на OCM у
    виклику більше немає (рев'ю живого смоуку 31.07.2026): `handle_location`
    (`app/handlers/user.py`) тепер запитує OCM ЗАВЖДИ, паралельно з
    TomTom, — обидва результати зливаються й дедупляться, тому обидва
    випадки (`None` і `[]`) виклик просто coalesce'ить у порожній список
    (`or []`) без спеціальної гілки.
    """
    if not is_enabled():
        return None

    if not _budget.try_consume():
        logger.warning(
            "TomTom: добову квоту запитів вичерпано (%s/добу) — фолбек на OCM",
            TOMTOM_DAILY_BUDGET,
        )
        return None

    url = "https://api.tomtom.com/search/2/nearbySearch/.json"
    params = {
        "key": TOMTOM_API_KEY,
        "lat": lat,
        "lon": lon,
        "radius": int(radius_km * 1000),
        "categorySet": TOMTOM_CATEGORY_EV_CHARGING,
        "limit": limit,
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params, timeout=8.0)
            if response.status_code != 200:
                logger.warning(
                    "TomTom nearbySearch: статус %s — фолбек на OCM", response.status_code
                )
                return None
            data = response.json()
    except httpx.TimeoutException:
        logger.warning("TomTom nearbySearch: таймаут — фолбек на OCM")
        return None
    except Exception:
        logger.error("TomTom nearbySearch: неочікувана помилка", exc_info=True)
        return None

    stations = []
    for result in data.get("results") or []:
        normalized = _normalize_search_result(result)
        if normalized:
            stations.append(normalized)
    return stations


async def get_availability(charging_availability_id):
    """
    Живий статус конекторів (available/occupied/outOfService) для КОНКРЕТНОЇ
    станції — окремий виклик, тому кличеться лише для станцій, що реально
    показуються водієві, а не для всіх знайдених.

    `None` — немає id, шар вимкнений, вичерпано квоту чи будь-яка помилка;
    виклику це не ламає, картка станції просто йде без live-статусу.
    """
    if not charging_availability_id or not is_enabled():
        return None

    if not _budget.try_consume():
        logger.warning(
            "TomTom: добову квоту запитів вичерпано (%s/добу) — статус станції пропущено",
            TOMTOM_DAILY_BUDGET,
        )
        return None

    url = "https://api.tomtom.com/search/2/chargingAvailability.json"
    # Параметр зветься "chargingAvailability", НЕ "chargingAvailabilityId" —
    # перевірено живим запитом 31.07.2026 (з "Id" TomTom відповідає 400).
    params = {"key": TOMTOM_API_KEY, "chargingAvailability": charging_availability_id}

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params, timeout=8.0)
            if response.status_code != 200:
                logger.warning(
                    "TomTom chargingAvailability: статус %s", response.status_code
                )
                return None
            data = response.json()
    except httpx.TimeoutException:
        logger.warning("TomTom chargingAvailability: таймаут")
        return None
    except Exception:
        logger.error("TomTom chargingAvailability: неочікувана помилка", exc_info=True)
        return None

    return _normalize_availability(data)
