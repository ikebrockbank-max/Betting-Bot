"""Funnel diagnostic for WNBA-hot: show how many combo props survive each
stage, so 0 picks is explained as 'no hot home players tonight' (legit) vs a
silent drop (bug). Mirrors _find_wnba_hot's logic with counters."""
import statistics as _st
import scanner_power_parlay as s
from data import wnba_rapidapi as w

lines = s.fetch_standard_lines(["WNBA"])
combos = [p for p in lines if p.get("stat_type") in ("Pts+Rebs+Asts", "Pts+Rebs")]
print(f"combo props: {len(combos)}")

inj = w.injured_names()
print("injured names known:", len(inj))

n_resolved = n_home = n_vals = n_over = n_hot = 0
samples = []
for pick in combos:
    info = w.resolve(pick["player"])
    if not info:
        continue
    n_resolved += 1
    if info.get("home_away") != "home":
        continue
    n_home += 1
    vals, mins = w.recent_combo_series(info["player_id"], pick["stat_type"], n=12)
    if not vals:
        if len(samples) < 8:
            samples.append((pick["player"], "NO VALS", info["player_id"]))
        continue
    n_vals += 1
    proj = w.project(vals, mins)
    if proj is None:
        continue
    direction = "OVER" if proj > pick["line"] else "UNDER"
    base = sum(vals[:10]) / len(vals[:10])
    trend = ((sum(vals[:3]) / len(vals[:3])) - base) / (base + 1e-9)
    if direction == "OVER":
        n_over += 1
    if len(samples) < 10:
        samples.append((pick["player"], f"line={pick['line']}", direction,
                        f"proj={proj}", f"trend={round(trend,3)}", f"L5={vals[:5]}"))
    if direction == "OVER" and trend > 0.15:
        n_hot += 1

print(f"\nFUNNEL: combos={len(combos)} -> resolved={n_resolved} -> home={n_home} "
      f"-> got_values={n_vals} -> OVER={n_over} -> hot(OVER&trend>0.15)={n_hot}")
print("\nsamples:")
for x in samples:
    print("  ", x)
