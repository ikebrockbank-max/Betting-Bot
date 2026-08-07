"""Dump the raw PrizePicks WNBA payload structure to answer: can PP data alone
power the WNBA-hot tier? We need (1) home/away and (2) recent game-by-game
values (the 'hot' trend). Prints included types, new_player attribute keys,
projection attribute keys, and sample description/team fields."""
import json
from collections import Counter
import scanner_power_parlay as s

lid = s.LEAGUE_IDS["WNBA"]
url = (f"https://api.prizepicks.com/projections"
       f"?league_id={lid}&per_page=500&single_stat=true&state_code=AZ")
data = s._get_pp_json(url, {"Referer": "https://app.prizepicks.com/"})

inc = data.get("included", [])
print("included types:", dict(Counter(o.get("type") for o in inc)))

np = next((o for o in inc if o.get("type") == "new_player"), None)
if np:
    print("\nnew_player attribute keys:", sorted(np.get("attributes", {}).keys()))
    print("new_player sample:", {k: np["attributes"].get(k) for k in
          ("name", "team", "team_name", "position", "league", "market") if k in np.get("attributes", {})})
    print("new_player relationships:", sorted((np.get("relationships") or {}).keys()))

# any game object?
game = next((o for o in inc if "game" in (o.get("type") or "").lower()), None)
print("\ngame-type included object:", game.get("type") if game else "NONE")
if game:
    print("game attribute keys:", sorted(game.get("attributes", {}).keys()))

proj = next((p for p in data.get("data", []) if p["attributes"].get("odds_type") == "standard"), None)
if proj:
    a = proj["attributes"]
    print("\nprojection attribute keys:", sorted(a.keys()))
    print("projection sample:", {k: a.get(k) for k in
          ("stat_type", "line_score", "description", "start_time", "game_id", "rank") if k in a})
    print("projection relationships:", sorted((proj.get("relationships") or {}).keys()))

# Does ANY field carry recent/historical game values?
allkeys = set()
for p in data.get("data", [])[:50]:
    allkeys |= set(p["attributes"].keys())
histish = [k for k in allkeys if any(w in k.lower() for w in
           ("recent", "last", "avg", "history", "game_log", "trend", "season", "l5", "l10"))]
print("\nany history/recent/avg fields on projections:", histish or "NONE")
