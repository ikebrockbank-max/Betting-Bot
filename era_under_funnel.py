"""Funnel the ERA-under tier so 0 picks is explained (no qualifying pitcher
today = legit) vs a too-strict/broken filter."""
import scanner_power_parlay as s

lines = s.fetch_standard_lines(["MLB"])
era = [p for p in lines if p.get("stat_type") == "Earned Runs Allowed"]
line25 = [p for p in era if float(p.get("line", 0) or 0) == 2.5]
print(f"Earned Runs Allowed props: {len(era)} | line 2.5: {len(line25)}")

n_scored = n_under = n_mild = 0
samples = []
for pick in line25:
    stats = s.get_stats_for_pick(pick)
    if not stats:
        continue
    r = s.score_pick(stats, pick)
    if r.get("skip_reason"):
        continue
    n_scored += 1
    d = r.get("direction")
    pf = float(r.get("park_factor", 0) or 0)
    if d == "UNDER":
        n_under += 1
    if d == "UNDER" and 0.95 <= pf < 1.0:
        n_mild += 1
    if len(samples) < 12:
        samples.append((pick["player"], d, round(pf, 2), r.get("home_away")))

print(f"scored={n_scored} -> UNDER={n_under} -> UNDER&park0.95-1.0={n_mild}")
print("samples (player, dir, park, home/away):")
for x in samples:
    print("  ", x)
