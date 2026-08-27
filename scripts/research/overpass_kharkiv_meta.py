"""РАЗОВИЙ дослідницький скрипт. Перевірка гіпотези "Харків = аномалія
через війну": тягне meta (дата останньої правки) для вже відомих 100
OSM node ID в радіусі 15км Харкова, щоб побачити, чи це довоєнний імпорт
без подальших правок, чи дані підтримуються й після 2022-02-24.

НЕ прод-код.
"""
import argparse
import json
import urllib.request
import urllib.parse

URL = "https://overpass-api.de/api/interpreter"
UA = {"User-Agent": "ev-charge-bot-research/1.0 (one-off coverage survey; hrutskiv@gmail.com)"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids-file", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    ids = open(args.ids_file).read().strip()
    query = f"""
    [out:json][timeout:180];
    node(id:{ids});
    out meta;
    """
    data = urllib.parse.urlencode({"data": query}).encode()
    req = urllib.request.Request(URL, data=data, headers=UA)
    with urllib.request.urlopen(req, timeout=170) as resp:
        result = json.loads(resp.read())

    elements = result.get("elements", [])
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(elements, f, ensure_ascii=False, indent=1)

    timestamps = sorted(e.get("timestamp", "") for e in elements)
    print(f"total={len(elements)}")
    if timestamps:
        print(f"earliest edit: {timestamps[0]}")
        print(f"latest edit:   {timestamps[-1]}")
        pre_war = sum(1 for t in timestamps if t < "2022-02-24")
        print(f"edits before 2022-02-24 (never touched since): {pre_war}/{len(timestamps)}")


if __name__ == "__main__":
    main()
