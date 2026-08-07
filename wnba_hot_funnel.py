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
    vals = w.recent_combo_values(info["player_id"], pick["stat_type"], n=10)
    if not vals:
        if len(samples) < 8:
            samples.append((pick["player"], "NO VALS", info["player_id"]))
        continue
    n_vals += 1
    r = s._compute_stats(pick["player"], pick["stat_type"], pick["line"], vals, "WNBA")
    if not r:
        if len(samples) < 8:
            samples.append((pick["player"], f"compute None (n={len(vals)})", vals[:5]))
        continue
    if r["direction"] == "OVER":
        n_over += 1
    if len(samples) < 8:
        samples.append((pick["player"], pick["line"], r["direction"],
                        f"trend={r['trend']}", f"avg={r['avg']}", f"L5={r['recent_values'][:5]}"))
    if r["direction"] == "OVER" and (r.get("trend") or 0) > 0.15:
        n_hot += 1

print(f"\nFUNNEL: combos={len(combos)} -> resolved={n_resolved} -> home={n_home} "
      f"-> got_values={n_vals} -> OVER={n_over} -> hot(OVER&trend>0.15)={n_hot}")
print("\nsamples:")
for x in samples:
    print("  ", x)
