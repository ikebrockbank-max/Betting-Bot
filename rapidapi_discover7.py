"""Dump ONE player-gamelog event fully so we extract PTS/REB/AST correctly."""
import os, json, urllib.request
KEY = os.getenv("RAPIDAPI_KEY", "")
HOST = "wnba-api.p.rapidapi.com"
H = {"x-rapidapi-host": HOST, "x-rapidapi-key": KEY}
with urllib.request.urlopen(urllib.request.Request(
        f"https://{HOST}/player-gamelog?playerId=2490553", headers=H), timeout=25) as r:
    d = json.loads(r.read())
gl = d["player_gamelog"]
print("labels:", gl["labels"])
print("names:", gl["names"])
events = gl["events"]
print("n events:", len(events))
# events may be dict{gameId:obj} — dump first obj fully
first_id = next(iter(events))
print("first event id:", first_id)
print("first event JSON:", json.dumps(events[first_id])[:1500])
# Is there a separate stats mapping? check for seasonTypes / statistics keys
print("\ntop-level player_gamelog keys:", list(gl.keys()))
for k in ("seasonTypes", "statistics", "categories"):
    if k in gl:
        print(f"  has {k}:", json.dumps(gl[k])[:500])
