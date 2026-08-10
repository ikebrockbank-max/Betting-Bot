"""Diagnose why build_player_index is empty: check RapidAPI quota remaining,
the raw schedule response keys vs the date key we look up, and index size."""
import os, json, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta
from data import wnba_rapidapi as w

KEY = os.getenv("RAPIDAPI_KEY", "")
HOST = "wnba-api.p.rapidapi.com"
H = {"x-rapidapi-host": HOST, "x-rapidapi-key": KEY}
et = datetime.now(timezone.utc) - timedelta(hours=4)
y, m, d = et.strftime("%Y"), et.strftime("%m"), et.strftime("%d")
print("ET date parts:", y, m, d, "-> lookup key", f"{y}{m}{d}")

url = f"https://{HOST}/wnbaschedule?year={y}&month={m}&day={d}"
try:
    with urllib.request.urlopen(urllib.request.Request(url, headers=H), timeout=25) as r:
        rl = {k: v for k, v in r.headers.items() if "requests" in k.lower()}
        data = json.loads(r.read())
        print("schedule 200; quota:", rl)
        print("schedule keys:", list(data.keys())[:12])
        key = f"{y}{m}{d}"
        print(f"games under [{key}]:", len(data.get(key, [])))
        if data.get(key):
            g = data[key][0]
            print("  sample game competitors:",
                  [(c.get("abbrev"), c.get("isHome"), c.get("id")) for c in g.get("competitors", [])])
except urllib.error.HTTPError as e:
    rl = {k: v for k, v in e.headers.items() if "requests" in k.lower()}
    print(f"schedule HTTP {e.code}; quota:", rl)

idx = w.build_player_index()
print("\nbuild_player_index size:", len(idx))
print("sample:", list(idx.items())[:2])
