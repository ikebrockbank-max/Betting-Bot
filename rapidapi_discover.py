"""Discover the RapidAPI wnba-api: verify the key works, learn the exact
response shapes for schedule (home/away) + player gamelog (hot trend), find
the player->id / roster endpoint, and read the rate-limit headers so we know
the free-tier budget before wiring the tier onto it."""
import os, json, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

KEY = os.getenv("RAPIDAPI_KEY", "")
HOST = "wnba-api.p.rapidapi.com"
H = {"x-rapidapi-host": HOST, "x-rapidapi-key": KEY}
print("key present:", bool(KEY), "| len:", len(KEY))


def call(path):
    url = f"https://{HOST}/{path}"
    try:
        req = urllib.request.Request(url, headers=H)
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read()
            rl = {k: v for k, v in r.headers.items() if "ratelimit" in k.lower()}
            return r.status, body, rl
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:300], {k: v for k, v in e.headers.items() if "ratelimit" in k.lower()}
    except Exception as e:
        return None, str(e).encode(), {}


def top(body):
    try:
        d = json.loads(body)
        if isinstance(d, dict):
            return "dict keys: " + ", ".join(list(d.keys())[:15])
        if isinstance(d, list):
            return f"list[{len(d)}]; item0 keys: " + ", ".join(list(d[0].keys())[:12] if d and isinstance(d[0], dict) else [])
    except Exception:
        return "non-json: " + body[:120].decode("utf-8", "replace")


et = datetime.now(timezone.utc) - timedelta(hours=4)
y, m, d = et.strftime("%Y"), et.strftime("%m"), et.strftime("%d")

# 1) schedule today
for path in [f"wnbaschedule?year={y}&month={m}&day={d}"]:
    st, body, rl = call(path)
    print(f"\n[{path}] -> {st} | ratelimit={rl}")
    print("  ", top(body))
    if st == 200:
        try:
            print("   RAW HEAD:", json.dumps(json.loads(body))[:700])
        except Exception:
            pass

# 2) roster / player-list endpoint candidates
for path in ["wnbateamplayers?teamid=3", "wnbateamroster?teamId=3",
             "wnbaplayerslist", "wnbaplayers", "teamplayers?teamId=3",
             "wnbateamlist", "wnbateams"]:
    st, body, rl = call(path)
    print(f"\n[{path}] -> {st}")
    if st == 200:
        print("   ", top(body))

# 3) player gamelog shape (A'ja Wilson espn id 2529121; try a couple)
for pid in ["2529121", "4066533"]:
    for path in [f"wnbaplayergamelog?playerId={pid}&type=WNBA",
                 f"wnbaplayergamelog?playerId={pid}"]:
        st, body, rl = call(path)
        print(f"\n[{path}] -> {st}")
        if st == 200:
            print("   ", top(body))
            try:
                print("   RAW HEAD:", json.dumps(json.loads(body))[:500])
            except Exception:
                pass
            break
