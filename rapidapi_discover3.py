"""Nail the wnbaplayergamelog param/format (last piece — the 'hot' trend).
Try variations with a known-active player, spaced to avoid 429."""
import os, json, time, urllib.request, urllib.error

KEY = os.getenv("RAPIDAPI_KEY", "")
HOST = "wnba-api.p.rapidapi.com"
H = {"x-rapidapi-host": HOST, "x-rapidapi-key": KEY}


def call(path, pause=3):
    time.sleep(pause)
    try:
        with urllib.request.urlopen(urllib.request.Request(f"https://{HOST}/{path}", headers=H), timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:160]
    except Exception as e:
        return None, str(e).encode()


PID = "3142191"  # Kelsey Mitchell, active
variants = [
    f"wnbaplayergamelog?playerId={PID}&type=WNBA",
    f"wnbaplayergamelog?playerId={PID}&type=wnba",
    f"wnbaplayergamelog?playerId={PID}",
    f"wnbaplayergamelog?id={PID}&type=WNBA",
    f"wnbaplayergamelog?player_id={PID}&type=WNBA",
    f"wnbaplayergamelog?playerId={PID}&type=WNBA&season=2026",
    f"playergamelog?playerId={PID}&type=WNBA",
    f"wnbaplayerstatistic?playerId={PID}",
    f"wnbaplayerstats?playerId={PID}",
]
for p in variants:
    st, body = call(p)
    tag = ""
    if st == 200:
        try:
            d = json.loads(body)
            tag = "KEYS: " + ", ".join(list(d.keys())[:12]) if isinstance(d, dict) else f"list[{len(d)}]"
        except Exception:
            tag = "200 non-json"
    print(f"[{p}] -> {st}  {tag}")
    if st == 200:
        print("   RAW HEAD:", json.dumps(json.loads(body))[:1200])
        break
