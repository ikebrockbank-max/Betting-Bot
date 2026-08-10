"""Does the STAR tier need a hitter-quality floor? Monasterio (a light bat)
qualified as a star and missed. The star def keys on line/p_over/pitcher/park
but NOT the hitter's own caliber. Bucket all resolved star picks by the
hitter's own signal (avg_val = their recent avg fantasy score at pick time)
and see if a floor separates hits from misses."""
from calibration_tracker import _sb_fetch
from daily_top_picks import _is_elite


def _f(r, k):
    try:
        return float(r.get(k) or 0)
    except (TypeError, ValueError):
        return 0.0


rows = _sb_fetch("select=player,stat_type,direction,line,p_over,pitcher_tier,"
                 "park_factor,avg_val,n_games,result,pick_date&resolved=eq.true")
stars = []
for r in rows:
    r["line"] = _f(r, "line"); r["park_factor"] = _f(r, "park_factor")
    r["avg_val"] = _f(r, "avg_val"); r["pitcher_tier"] = r.get("pitcher_tier") or ""
    if r.get("result") in ("hit", "miss") and _is_elite(r):
        stars.append(r)

print(f"resolved STAR picks: {len(stars)}")
if not stars:
    raise SystemExit(0)

# overall
h = sum(1 for r in stars if r["result"] == "hit")
print(f"overall: {h}/{len(stars)} = {h/len(stars):.1%}\n")

# bucket by avg_val (the hitter's own recent fantasy-score average at pick time)
# vs the line. A weak bat has avg_val well below the line even in a soft spot.
print("by (avg_val - line) — how far the hitter's own recent avg sits vs the line:")
buckets = [(-99, -1.5, "avg 1.5+ BELOW line"), (-1.5, -0.5, "0.5-1.5 below"),
           (-0.5, 0.5, "within 0.5"), (0.5, 1.5, "0.5-1.5 above"),
           (1.5, 99, "1.5+ ABOVE line")]
for lo, hi, lbl in buckets:
    b = [r for r in stars if lo <= (r["avg_val"] - r["line"]) < hi]
    if b:
        bh = sum(1 for r in b if r["result"] == "hit")
        print(f"  {lbl:22} {bh:2}/{len(b):2} = {bh/len(b):.0%}")

# raw avg_val buckets
print("\nby raw avg_val (recent fantasy-score average):")
for lo, hi in [(0, 5), (5, 6), (6, 7), (7, 8), (8, 99)]:
    b = [r for r in stars if lo <= r["avg_val"] < hi]
    if b:
        bh = sum(1 for r in b if r["result"] == "hit")
        print(f"  avg_val {lo}-{hi}: {bh:2}/{len(b):2} = {bh/len(b):.0%}")

# lowest-avg_val stars (the 'weak bat' cases like Monasterio)
print("\nweakest-bat stars (lowest avg_val), result:")
for r in sorted(stars, key=lambda x: x["avg_val"])[:10]:
    mark = "✅" if r["result"] == "hit" else "❌"
    print(f"  {mark} {r['player'][:20]:20} line {r['line']} avg_val {r['avg_val']:.1f} "
          f"p_over {r.get('p_over')} {r['pick_date']}")
