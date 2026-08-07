"""Hyphenated routes are the live ones. Test player-gamelog (the 'hot' trend),
the correct box-score route, and dump player-statistic content to see if it
carries recent/game-by-game form. Spaced to avoid 429."""
import os, json, time, urllib.request, urllib.error

KEY = os.getenv("RAPIDAPI_KEY", "")
HOST = "wnba-api.p.rapidapi.com"
H = {"x-rapidapi-host": HOST, "x-rapidapi-key": KEY}


def call(path, pause=6):
    time.sleep(pause)
    try:
        with urllib.request.urlopen(urllib.request.Request(f"https://{HOST}/{path}", headers=H), timeout=25) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:160]
    except Exception as e:
        return None, str(e).encode()


PID = "2490553"  # Griner
tests = [
    f"player-gamelog?playerId={PID}&type=WNBA",
    f"player-gamelog?playerId={PID}",
    f"player-game-log?playerId={PID}&type=WNBA",
    "box-score?id=401857119",
    "box-score?gameId=401857119",
]
for i, path in enumerate(tests):
    st, body = call(path, pause=0 if i == 0 else 6)
    tag = ""
    if st == 200:
        try:
            d = json.loads(body)
            tag = "KEYS: " + ", ".join(list(d.keys())[:14]) if isinstance(d, dict) else f"list[{len(d)}]"
        except Exception:
            tag = "200 non-json"
    print(f"[{path}] -> {st}  {tag}")
    if st == 200 and ("gamelog" in path or "game-log" in path or "box" in path):
        print("   RAW HEAD:", json.dumps(json.loads(body))[:1500])

# Dump player-statistic to see if categories hold recent form
st, body = call(f"player-statistic?playerId={PID}")
if st == 200:
    d = json.loads(body)
    cats = d.get("categories", [])
    print(f"\nplayer-statistic categories ({len(cats)}):")
    for c in cats[:6]:
        nm = c.get("name")
        stnames = [s.get("name") for s in c.get("statistics", c.get("stats", []))][:8]
        print(f"  {nm}: {stnames}")
    print("RAW HEAD:", json.dumps(d)[:600])
