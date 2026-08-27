import urllib.request
import urllib.parse

query = """
[out:json][timeout:180];
area["ISO3166-1"="UA"][admin_level=2]->.ua;
(
  node["amenity"="charging_station"](area.ua);
  way["amenity"="charging_station"](area.ua);
);
out count;
"""

url = "https://overpass-api.de/api/interpreter"
data = urllib.parse.urlencode({"data": query}).encode()
req = urllib.request.Request(url, data=data, headers={
    "User-Agent": "ev-charge-bot-research/1.0 (one-off coverage survey; hrutskiv@gmail.com)"
})
try:
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = resp.read().decode()
        print(body)
except Exception as e:
    print("ERROR:", type(e).__name__, e)
