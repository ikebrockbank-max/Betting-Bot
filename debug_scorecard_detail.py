"""Reproduce the 08-11 scorecard pick-by-pick: for every logged 08-11 pick,
show the tier bucket, the graded actual, and hit/miss/void — so we can see
exactly whether any Soderstrom (DNP) pick was counted as a hit."""
from calibration_tracker import _sb_fetch, _fetch_actual_mlb
from daily_top_picks import _is_elite, _is_prime
from daily_scorecard import _is_lock, _f

DATE = "2026-08-11"
rows = _sb_fetch(f"select=player,sport,stat_type,direction,line,confidence,p_over,"
                 f"hit_rate,pitcher_tier,edge_pct,park_factor,result,pick_date"
                 f"&pick_date=eq.{DATE}")
for r in rows:
    r["line"] = _f(r, "line"); r["hit_rate"] = _f(r, "hit_rate")
    r["edge_pct"] = _f(r, "edge_pct"); r["park_factor"] = _f(r, "park_factor")
    r["pitcher_tier"] = r.get("pitcher_tier") or ""

print(f"08-11 logged picks: {len(rows)}\n")
star_hits = star_n = lock_hits = lock_n = 0
for r in rows:
    if r.get("sport") != "MLB":
        continue
    look = r["stat_type"].replace(" (Goblin)", "")
    a = _fetch_actual_mlb(r["player"], look, DATE)
    if a in (None, "DNP"):
        verdict = f"VOID/pending ({a})"
        graded = None
    else:
        graded = (a > r["line"]) if r["direction"] == "OVER" else (a < r["line"])
        verdict = f"actual={a} -> {'HIT' if graded else 'miss'}"
    tier = "PRIME" if _is_prime(r) else "STAR" if _is_elite(r) else "LOCK" if _is_lock(r) else "-"
    # only print stars/locks and anything involving Soderstrom
    if tier in ("STAR", "PRIME", "LOCK") or "Soderstrom" in r["player"]:
        print(f"  [{tier:5}] {r['player'][:18]:18} {r['direction']} {r['line']} "
              f"{look[:22]:22} | {verdict}")
    if tier in ("STAR", "PRIME") and graded is not None:
        star_n += 1; star_hits += 1 if graded else 0
    if tier == "LOCK" and graded is not None:
        lock_n += 1; lock_hits += 1 if graded else 0

print(f"\nStars counted: {star_hits}/{star_n} | Locks counted: {lock_hits}/{lock_n}")
