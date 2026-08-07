"""Live probe: why has the WNBA-hot tier never fired? Checks two things:
  1. Are there WNBA combo props available right now? (schedule / fetch)
  2. When scored, does home_away actually resolve to home/away, or is it
     stuck on 'unknown' (which the _find_wnba_hot gate rejects)?
Caps scored picks to keep partner-API rate limits sane.
"""
from collections import Counter
import scanner_power_parlay as s

try:
    lines = s.fetch_standard_lines(["WNBA"])
except Exception as e:
    print(f"fetch_standard_lines FAILED: {e}")
    raise SystemExit

combo = [p for p in lines if p.get("stat_type") in ("Pts+Rebs+Asts", "Pts+Rebs")]
print(f"WNBA props fetched: {len(lines)}  | combo (PRA/PR): {len(combo)}")
if not lines:
    print(">> No WNBA props available today (schedule break or empty fetch).")
    raise SystemExit

ha_all, hot_home, scored = Counter(), 0, 0
for pick in combo[:20]:
    stats = s.get_stats_for_pick(pick)
    if not stats:
        continue
    r = s.score_pick(stats, pick)
    scored += 1
    ha = (r.get("home_away") or "unknown").lower()
    ha_all[ha] += 1
    over = r.get("direction") == "OVER"
    hot = (r.get("trend") or 0) > 0.15
    if over and hot and ha == "home":
        hot_home += 1
        print(f"  QUALIFIES: {r.get('player')} {r.get('stat_type')} "
              f"trend={r.get('trend'):.2f} p_over={r.get('p_over')}")

print(f"\nscored {scored} combo picks")
print(f"home_away resolution: {dict(ha_all)}")
print(f"combo picks that would clear the WNBA-hot gate (OVER+hot+home): {hot_home}")
if ha_all and ha_all.get("home", 0) == 0 and ha_all.get("away", 0) == 0:
    print(">> BUG CONFIRMED: home_away never resolves — gate rejects everything.")
elif ha_all.get("home", 0) or ha_all.get("away", 0):
    print(">> home_away IS resolving; tier is just strict (few qualify).")
