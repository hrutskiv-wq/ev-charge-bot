"""РАЗОВИЙ дослідницький скрипт (Фаза 0 роадмапу, карта-довідник).

Рахує POI OpenChargeMap по Україні з репозиторію ocm-export
(https://github.com/openchargemap/ocm-export, один файл на POI), окремо
загальну кількість і кількість тих, чий DataProvider має
IsOpenDataLicensed=true (умова легального використання, CC BY 4.0 тощо).

Репо ocm-export ~350 МБ і сотні тисяч дрібних файлів по всіх країнах —
повний git-клон таймаутить. Замість цього тягнемо лише data/UA через
GitHub REST API (raw.githubusercontent.com) — 572 POI-файли для UA
станом на 2026-07-30, під ліміт 1000 записів contents-API (пагінація
не потрібна).

НЕ прод-код. Результати нікуди не пишуться постійно (кеш у --cache-dir
лише для повторних прогонів цієї сесії).
"""
import argparse
import json
import os
import time
import urllib.request

REPO = "openchargemap/ocm-export"
RAW_BASE = f"https://raw.githubusercontent.com/{REPO}/main"
API_BASE = f"https://api.github.com/repos/{REPO}/contents"
UA = {"User-Agent": "ev-charge-bot-research/1.0 (one-off coverage survey; hrutskiv@gmail.com)"}


def fetch(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def fetch_json(url):
    return json.loads(fetch(url))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", required=True)
    ap.add_argument("--country", default="UA")
    args = ap.parse_args()

    os.makedirs(args.cache_dir, exist_ok=True)
    refdata_path = os.path.join(args.cache_dir, "referencedata.json")
    if not os.path.exists(refdata_path):
        with open(refdata_path, "wb") as f:
            f.write(fetch(f"{RAW_BASE}/data/referencedata.json"))
    refdata = json.load(open(refdata_path, encoding="utf-8"))
    opendata_by_provider = {
        p["ID"]: bool(p.get("IsOpenDataLicensed")) for p in refdata["DataProviders"]
    }

    listing_path = os.path.join(args.cache_dir, f"{args.country}_listing.json")
    if not os.path.exists(listing_path):
        with open(listing_path, "wb") as f:
            f.write(fetch(f"{API_BASE}/data/{args.country}?per_page=100"))
    listing = json.load(open(listing_path, encoding="utf-8"))
    if isinstance(listing, dict):
        raise RuntimeError(f"GitHub API returned an error for {args.country}: {listing}")

    poi_dir = os.path.join(args.cache_dir, args.country)
    os.makedirs(poi_dir, exist_ok=True)

    pois = []
    for i, entry in enumerate(listing):
        name = entry["name"]
        local_path = os.path.join(poi_dir, name)
        if not os.path.exists(local_path):
            body = fetch(f"{RAW_BASE}/data/{args.country}/{name}")
            with open(local_path, "wb") as f:
                f.write(body)
            time.sleep(0.05)
        pois.append(json.load(open(local_path, encoding="utf-8")))
        if (i + 1) % 100 == 0:
            print(f"...{i + 1}/{len(listing)}")

    total = len(pois)
    opendata_true = sum(1 for p in pois if opendata_by_provider.get(p.get("DataProviderID")))
    opendata_false_or_unknown = total - opendata_true

    print(f"\nOCM {args.country}: total={total}")
    print(f"OCM {args.country}: opendata=true (IsOpenDataLicensed) = {opendata_true}")
    print(f"OCM {args.country}: opendata=false/unknown = {opendata_false_or_unknown}")

    by_provider = {}
    for p in pois:
        pid = p.get("DataProviderID")
        by_provider[pid] = by_provider.get(pid, 0) + 1
    print("\nРозбивка по DataProviderID (count, IsOpenDataLicensed, Title):")
    provider_titles = {p["ID"]: p["Title"] for p in refdata["DataProviders"]}
    for pid, cnt in sorted(by_provider.items(), key=lambda x: -x[1]):
        print(f"  {pid}: {cnt}  opendata={opendata_by_provider.get(pid)}  {provider_titles.get(pid)}")

    out_path = os.path.join(args.cache_dir, f"{args.country}_pois_flat.json")
    flat = []
    for p in pois:
        addr = p.get("AddressInfo") or {}
        flat.append({
            "ID": p.get("ID"),
            "DataProviderID": p.get("DataProviderID"),
            "opendata": opendata_by_provider.get(p.get("DataProviderID"), False),
            "lat": addr.get("Latitude"),
            "lon": addr.get("Longitude"),
            "title": addr.get("Title"),
        })
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(flat, f, ensure_ascii=False, indent=1)
    print(f"\nПлаский список збережено: {out_path}")


if __name__ == "__main__":
    main()
