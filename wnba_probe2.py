"""Decisive WNBA-hot probe: for each combo prop today, show the exact stage
it dies at — no stats, skip_reason, direction, trend, or the home gate — so
we can say definitively whether _find_wnba_hot is BUGGED or just starved of
game-days."""
from collections import Counter
import scanner_power_parlay as s

lines = s.fetch_standard_lines(["WNBA"])
combos = [p for p in lines if p.get("stat_type") in ("Pts+Rebs+Asts", "Pts+Rebs")]
print(f"WNBA props: {len(lines)} | combos: {len(combos)}")

reasons = Counter()
ha_vals = Counter()
scored, over_hot, home_ok = 0, 0, 0
samples = []
for pick in combos:
    stats = s.get_stats_for_pick(pick)
    if not stats:
        reasons["no_stats(get_stats_for_pick=None)"] += 1
        continue
    r = s.score_pick(stats, pick)
    if r.get("skip_reason"):
        reasons[f"skip:{r['skip_reason']}"] += 1
        continue
    scored += 1
    ha = (r.get("home_away") or "∅")
    ha_vals[ha] += 1
    if r.get("direction") == "OVER" and (r.get("trend") or 0) > 0.15:
        over_hot += 1
        if ha.lower() == "home":
            home_ok += 1
    if len(samples) < 8:
        samples.append((r.get("player"), r.get("direction"),
                        round(r.get("trend") or 0, 2), ha))

print(f"\nscored: {scored} | OVER+hot: {over_hot} | OVER+hot+HOME: {home_ok}")
print("\ndeath stage (why not scored):")
for k, v in reasons.most_common():
    print(f"  {v:3d}  {k}")
print("\nhome_away values among scored picks:")
for k, v in ha_vals.most_common():
    print(f"  {v:3d}  {k}")
print("\nsample scored combos (player, dir, trend, home_away):")
for row in samples:
    print("  ", row)
