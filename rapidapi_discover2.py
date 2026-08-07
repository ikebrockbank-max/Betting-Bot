"""Confirm the two remaining RapidAPI wnba-api response shapes with SPACED
calls (free tier throttles per-second): wnbateamplayers (roster -> player id)
and wnbaplayergamelog (recent game values for the 'hot' trend)."""
import os, json, time, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

KEY = os.getenv("RAPIDAPI_KEY", "")
HOST = "wnba-api.p.rapidapi.com"
H = {"x-rapidapi-host": HOST, "x-rapidapi-key": KEY}


def call(path, pause=2.5):
    time.sleep(pause)
    url = f"https://{HOST}/{path}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=H), timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:200]
    except Exception as e:
        return None, str(e).encode()


et = datetime.now(timezone.utc) - timedelta(hours=4)
y, m, d = et.strftime("%Y"), et.strftime("%m"), et.strftime("%d")

# schedule -> pull a team id playing today
st, body = call(f"wnbaschedule?year={y}&month={m}&day={d}", pause=0)
sched = json.loads(body)
day = sched.get(f"{y}{m}{d}") or next(iter(sched.values()))
game = day[0]
tid = game["competitors"][0]["id"]
print("today game:", game["competitors"][0]["abbrev"], "isHome", game["competitors"][0]["isHome"],
      "vs", game["competitors"][1]["abbrev"], "| teamid", tid)

# roster
st, body = call(f"wnbateamplayers?teamid={tid}")
print(f"\nwnbateamplayers?teamid={tid} -> {st}")
if st == 200:
    d0 = json.loads(body)
    print("  top keys:", list(d0.keys()))
    print("  RAW HEAD:", json.dumps(d0)[:900])

# find a player id from roster to test gamelog
pid = None
try:
    team = json.loads(body).get("team", {})
    ath = team.get("athletes") or team.get("players") or []
    if ath and isinstance(ath[0], dict) and "items" in ath[0]:
        ath = ath[0]["items"]
    for a in ath:
        pid = a.get("id") or a.get("playerId")
        if pid:
            print("  sample player:", a.get("displayName") or a.get("fullName") or a.get("name"), "id", pid)
            break
except Exception as e:
    print("  roster parse err:", e)

if pid:
    st, body = call(f"wnbaplayergamelog?playerId={pid}&type=WNBA")
    print(f"\nwnbaplayergamelog?playerId={pid}&type=WNBA -> {st}")
    if st == 200:
        d0 = json.loads(body)
        print("  top keys:", list(d0.keys()) if isinstance(d0, dict) else f"list[{len(d0)}]")
        print("  RAW HEAD:", json.dumps(d0)[:1100])
