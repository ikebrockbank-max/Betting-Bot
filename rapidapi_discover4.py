"""Final gamelog nail-down: documented route with unmistakable star ESPN ids,
6s spacing to fully avoid 429. Also dump the roster athlete structure so the
name->id parse is confirmed. Budget-conscious: ~8 spaced calls."""
import os, json, time, urllib.request, urllib.error

KEY = os.getenv("RAPIDAPI_KEY", "")
HOST = "wnba-api.p.rapidapi.com"
H = {"x-rapidapi-host": HOST, "x-rapidapi-key": KEY}


def call(path, pause=6):
    time.sleep(pause)
    try:
        with urllib.request.urlopen(urllib.request.Request(f"https://{HOST}/{path}", headers=H), timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:160]
    except Exception as e:
        return None, str(e).encode()


# Confirm roster athlete structure once (Connecticut = 18)
st, body = call("wnbateamplayers?teamid=18", pause=0)
if st == 200:
    team = json.loads(body).get("team", {})
    print("team keys:", list(team.keys()))
    ath = team.get("athletes", [])
    print("athletes type:", type(ath).__name__, "len", len(ath) if isinstance(ath, list) else "?")
    if ath:
        a0 = ath[0]
        if isinstance(a0, dict) and "items" in a0:
            print("  grouped; group keys:", list(a0.keys()), "| first item:", json.dumps(a0["items"][0])[:200])
        else:
            print("  flat; first athlete:", json.dumps(a0)[:220])

# Gamelog with real stars
STARS = {"AjaWilson": "3149391", "CaitlinClark": "4433403", "KelseyMitchell": "3142191"}
for nm, pid in STARS.items():
    for path in [f"wnbaplayergamelog?playerId={pid}&type=WNBA", f"wnbaplayergamelog?playerId={pid}"]:
        st, body = call(path)
        ok = ""
        if st == 200:
            try:
                d = json.loads(body)
                ok = "KEYS: " + ", ".join(list(d.keys())[:14]) if isinstance(d, dict) else f"list[{len(d)}]"
            except Exception:
                ok = "200 non-json"
        print(f"[{nm}] {path} -> {st}  {ok}")
        if st == 200:
            print("   RAW HEAD:", json.dumps(json.loads(body))[:1400])
            raise SystemExit(0)
