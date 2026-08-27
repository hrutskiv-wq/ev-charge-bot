"""РАЗОВИЙ дослідницький скрипт (Фаза 0 роадмапу, карта-довідник).

Перетин OCM (data/UA з ocm-export, зібрано ocm_ua_count.py) і OSM
(amenity=charging_station, зібрано overpass_full.py) — скільки OSM-станцій
мають OCM-сусіда в радіусі ~100 м. Дає ЧИСТИЙ приріст OSM понад OCM
(наскільки OSM додає покриття, а не просто дублює OCM), не валову суму
двох джерел.

Haversine з app/services/geo.py (та сама функція, що вже використовується
в проді для пошуку станцій поруч) — не переписуємо метрику руками.

НЕ прод-код.
"""
import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.services.geo import haversine_km

THRESHOLD_KM = 0.1  # ~100 м


def osm_latlon(el):
    if el["type"] == "node":
        return el.get("lat"), el.get("lon")
    center = el.get("center") or {}
    return center.get("lat"), center.get("lon")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ocm", required=True, help="UA_pois_flat.json з ocm_ua_count.py")
    ap.add_argument("--osm", required=True, help="raw elements.json з overpass_full.py")
    ap.add_argument("--sample-out", required=True)
    ap.add_argument("--sample-size", type=int, default=10)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    ocm = json.load(open(args.ocm, encoding="utf-8"))
    ocm_points = [(p["lat"], p["lon"]) for p in ocm if p.get("lat") is not None and p.get("lon") is not None]

    osm_elements = json.load(open(args.osm, encoding="utf-8"))
    osm_points = []
    for el in osm_elements:
        lat, lon = osm_latlon(el)
        if lat is None or lon is None:
            continue
        osm_points.append((el, lat, lon))

    matched = 0
    unmatched = []
    for el, lat, lon in osm_points:
        has_ocm_neighbor = False
        for olat, olon in ocm_points:
            if haversine_km(lat, lon, olat, olon) <= THRESHOLD_KM:
                has_ocm_neighbor = True
                break
        if has_ocm_neighbor:
            matched += 1
        else:
            unmatched.append(el)

    total_osm = len(osm_points)
    net_new = total_osm - matched

    print(f"OCM points (with coords): {len(ocm_points)}")
    print(f"OSM points (with coords): {total_osm}")
    print(f"OSM matched to an OCM neighbor within {THRESHOLD_KM * 1000:.0f} m: {matched}")
    print(f"OSM net-new over OCM (no OCM neighbor within {THRESHOLD_KM * 1000:.0f} m): {net_new}")

    random.seed(args.seed)
    sample = random.sample(osm_elements, min(args.sample_size, len(osm_elements)))
    with open(args.sample_out, "w", encoding="utf-8") as f:
        json.dump(sample, f, ensure_ascii=False, indent=1)
    print(f"\nВибірка {len(sample)} випадкових OSM-записів -> {args.sample_out}")


if __name__ == "__main__":
    main()
