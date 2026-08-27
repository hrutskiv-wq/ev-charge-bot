"""РАЗОВИЙ дослідницький скрипт (Фаза 0 роадмапу, карта-довідник), вимір 2.

Покриття по 8 містах на вже завантажених даних OCM+OSM (кеш попереднього
запуску ocm_ua_count.py / overpass_full.py — мережу вдруге не смикаємо):
для кожного міста в радіусі 15 км від центру рахує OCM, OSM і
дедуплікований (100 м, haversine з app/services/geo.py) об'єднаний набір,
плюс скільки з об'єднаного набору мають ХОЧ ЯКІСЬ корисні метадані
(конектор АБО потужність АБО оператор). Для Львова додатково пише CSV
з повним списком знайдених станцій для ручної звірки.

Дедуплікація — union-find по парах точок ближче 100 м (не лише "OSM має
OCM-сусіда", а повне злиття кластерів через обидва джерела разом), щоб
трикутник OCM-OSM-OSM у межах 100 м не порахувався за дві станції.

Евристика "є оператор" для OCM: OperatorID присутній і != 1
("Unknown Operator" — плейсхолдер OCM, не справжня назва). Для OSM —
присутній тег operator.

НЕ прод-код.
"""
import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.services.geo import haversine_km

RADIUS_KM = 15.0
DEDUPE_KM = 0.1  # ~100 м

CITIES = [
    ("Київ", 50.4501, 30.5234),
    ("Львів", 49.8397, 24.0297),
    ("Одеса", 46.4825, 30.7233),
    ("Харків", 49.9935, 36.2304),
    ("Дніпро", 48.4647, 35.0462),
    ("Вінниця", 49.2331, 28.4682),
    ("Івано-Франківськ", 48.9226, 24.7111),
    ("Ужгород", 48.6208, 22.2879),
]

UNKNOWN_OPERATOR_ID = 1


def load_ocm_points(ocm_dir, referencedata_path):
    refdata = json.load(open(referencedata_path, encoding="utf-8"))
    operator_titles = {o["ID"]: o["Title"] for o in refdata["Operators"]}

    points = []
    for path in sorted(Path(ocm_dir).glob("OCM-*.json")):
        poi = json.load(open(path, encoding="utf-8"))
        addr = poi.get("AddressInfo") or {}
        lat, lon = addr.get("Latitude"), addr.get("Longitude")
        if lat is None or lon is None:
            continue
        connections = poi.get("Connections") or []
        has_connector = any(c.get("ConnectionTypeID") for c in connections)
        has_power = any(c.get("PowerKW") for c in connections)
        operator_id = poi.get("OperatorID")
        has_operator = bool(operator_id) and operator_id != UNKNOWN_OPERATOR_ID
        points.append({
            "source": "OCM",
            "lat": lat,
            "lon": lon,
            "name": addr.get("Title"),
            "operator": operator_titles.get(operator_id) if has_operator else None,
            "address": addr.get("AddressLine1"),
            "has_metadata": has_connector or has_power or has_operator,
        })
    return points


def osm_latlon(el):
    if el["type"] == "node":
        return el.get("lat"), el.get("lon")
    center = el.get("center") or {}
    return center.get("lat"), center.get("lon")


def load_osm_points(osm_raw_path):
    elements = json.load(open(osm_raw_path, encoding="utf-8"))
    points = []
    for el in elements:
        lat, lon = osm_latlon(el)
        if lat is None or lon is None:
            continue
        tags = el.get("tags") or {}
        has_connector = any(k.startswith("socket:") and not k.count(":") > 1 for k in tags)
        has_power = any(k.startswith("socket:") and k.endswith(":output") for k in tags)
        has_operator = bool(tags.get("operator"))
        addr_parts = [tags.get("addr:street"), tags.get("addr:housenumber"), tags.get("addr:city")]
        address = ", ".join(p for p in addr_parts if p)
        points.append({
            "source": "OSM",
            "lat": lat,
            "lon": lon,
            "name": tags.get("name"),
            "operator": tags.get("operator"),
            "address": address or None,
            "has_metadata": has_connector or has_power or has_operator,
        })
    return points


def within_radius(points, center_lat, center_lon, radius_km):
    return [p for p in points if haversine_km(p["lat"], p["lon"], center_lat, center_lon) <= radius_km]


def dedupe_union_find(points, threshold_km):
    n = len(points)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(n):
        for j in range(i + 1, n):
            if haversine_km(points[i]["lat"], points[i]["lon"], points[j]["lat"], points[j]["lon"]) <= threshold_km:
                union(i, j)

    clusters = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(points[i])
    return list(clusters.values())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ocm-dir", required=True, help="ocm_cache/UA directory")
    ap.add_argument("--referencedata", required=True)
    ap.add_argument("--osm-raw", required=True)
    ap.add_argument("--lviv-csv", required=True)
    args = ap.parse_args()

    all_ocm = load_ocm_points(args.ocm_dir, args.referencedata)
    all_osm = load_osm_points(args.osm_raw)
    print(f"Завантажено: OCM={len(all_ocm)} (з координатами), OSM={len(all_osm)} (з координатами)\n")

    rows = []
    for city, clat, clon in CITIES:
        ocm_in = within_radius(all_ocm, clat, clon, RADIUS_KM)
        osm_in = within_radius(all_osm, clat, clon, RADIUS_KM)
        combined = ocm_in + osm_in
        clusters = dedupe_union_find(combined, DEDUPE_KM)
        merged_count = len(clusters)
        with_metadata = sum(1 for c in clusters if any(p["has_metadata"] for p in c))
        rows.append((city, len(ocm_in), len(osm_in), merged_count, with_metadata))

        if city == "Львів":
            lviv_clusters = clusters

    print(f"{'Misto':<20}{'OCM':>6}{'OSM':>6}{'Merged':>13}{'WithMetadata':>15}")
    for city, ocm_n, osm_n, merged, meta in rows:
        print(f"{city:<20}{ocm_n:>6}{osm_n:>6}{merged:>13}{meta:>15}")

    with open(args.lviv_csv, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["назва", "оператор", "адреса", "lat", "lon", "джерело(а)", "є_метадані"])
        for cluster in sorted(lviv_clusters, key=lambda c: c[0]["lat"]):
            sources = "+".join(sorted(set(p["source"] for p in cluster)))
            has_meta = any(p["has_metadata"] for p in cluster)
            name = next((p["name"] for p in cluster if p["name"]), "")
            operator = next((p["operator"] for p in cluster if p["operator"]), "")
            address = next((p["address"] for p in cluster if p["address"]), "")
            lat = cluster[0]["lat"]
            lon = cluster[0]["lon"]
            w.writerow([name, operator, address, f"{lat:.6f}", f"{lon:.6f}", sources, "так" if has_meta else "ні"])
    print(f"\nЛьвів — {len(lviv_clusters)} станцій -> {args.lviv_csv}")


if __name__ == "__main__":
    main()
