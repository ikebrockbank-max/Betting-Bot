"""Confirm box-score structure (wnbabox?id=) — our source for recent per-game
player values (PTS/REB/AST) since there's no gamelog route. Also test a couple
plausible player-statistic shortcuts. Spaced to avoid 429."""
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


# Box score for a known completed game (IND vs LV, 20260806)
st, body = call("wnbabox?id=401857119", pause=0)
print(f"wnbabox?id=401857119 -> {st}")
if st == 200:
    d = json.loads(body)
    print("top keys:", list(d.keys()) if isinstance(d, dict) else type(d).__name__)
    # walk to find player rows with stat values
    def peek(o, depth=0, path="root"):
        if depth > 4:
            return
        if isinstance(o, dict):
            ks = list(o.keys())
            if any(k in ks for k in ("athletes", "players", "statistics", "stats")):
                print(f"  [{path}] keys: {ks[:12]}")
            for k in ("players", "athletes", "teams", "boxscore", "statistics"):
                if k in o:
                    peek(o[k], depth+1, f"{path}.{k}")
        elif isinstance(o, list) and o:
            print(f"  [{path}] list[{len(o)}] item0 keys: {list(o[0].keys())[:12] if isinstance(o[0],dict) else o[0]}")
            peek(o[0], depth+1, f"{path}[0]")
    peek(d)
    print("RAW HEAD:", json.dumps(d)[:900])

# Try a player-statistic shortcut (season splits might carry recent form)
for path in ["player-statistic?playerId=2490553", "wnbaplayerstatistic?playerId=2490553"]:
    st, body = call(path)
    print(f"\n{path} -> {st}")
    if st == 200:
        print("  KEYS:", list(json.loads(body).keys())[:14])
