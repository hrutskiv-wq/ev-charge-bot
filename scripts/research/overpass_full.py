"""РАЗОВИЙ дослідницький скрипт (Фаза 0 роадмапу, карта-довідник).

Тягне ПОВНІ дані (координати + теги) amenity=charging_station по Україні
з Overpass API — не лише count (див. overpass_count.py). Зберігає сирий
JSON у --out для подальшого перетину з OCM і вибірки якості.

Публічний overpass-api.de інколи віддає 504 під навантаженням — просто
перезапустити.

НЕ прод-код.
"""
import argparse
import json
import urllib.request
import urllib.parse

QUERY = """
[out:json][timeout:180];
area["ISO3166-1"="UA"][admin_level=2]->.ua;
(
  node["amenity"="charging_station"](area.ua);
  way["amenity"="charging_station"](area.ua);
);
out body center;
"""

URL = "https://overpass-api.de/api/interpreter"
UA = {"User-Agent": "ev-charge-bot-research/1.0 (one-off coverage survey; hrutskiv@gmail.com)"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    data = urllib.parse.urlencode({"data": QUERY}).encode()
    req = urllib.request.Request(URL, data=data, headers=UA)
    with urllib.request.urlopen(req, timeout=170) as resp:
        body = resp.read()

    result = json.loads(body)
    elements = result.get("elements", [])
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(elements, f, ensure_ascii=False, indent=1)

    nodes = sum(1 for e in elements if e["type"] == "node")
    ways = sum(1 for e in elements if e["type"] == "way")
    print(f"nodes={nodes} ways={ways} total={len(elements)}")
    print(f"saved -> {args.out}")


if __name__ == "__main__":
    main()
