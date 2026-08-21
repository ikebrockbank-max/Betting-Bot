"""Break down all resolved LOCK picks by stat type + line (Fantasy Score vs
Hits vs …), and report the ERA-under tier's live record since implementation
(2026-08-13)."""
from collections import defaultdict
from calibration_tracker import _sb_fetch


def _f(r, k):
    try:
        return float(r.get(k) or 0)
    except (TypeError, ValueError):
        return 0.0


rows = _sb_fetch("select=player,stat_type,direction,line,result,pick_date"
                 "&resolved=eq.true")
rows = [r for r in rows if r.get("result") in ("hit", "miss")]

locks = [r for r in rows if "(Goblin)" in (r.get("stat_type") or "")]
h = sum(1 for r in locks if r["result"] == "hit")
print(f"=== LOCKS total: {h}/{len(locks)} = {h/len(locks):.0%} ===")

# by base stat type
by_stat = defaultdict(lambda: [0, 0])
by_stat_line = defaultdict(lambda: [0, 0])
for r in locks:
    base = (r.get("stat_type") or "").replace(" (Goblin)", "")
    by_stat[base][1] += 1
    by_stat[base][0] += 1 if r["result"] == "hit" else 0
    key = (base, _f(r, "line"))
    by_stat_line[key][1] += 1
    by_stat_line[key][0] += 1 if r["result"] == "hit" else 0

print("\nby stat type:")
for base in sorted(by_stat, key=lambda x: -by_stat[x][1]):
    hh, nn = by_stat[base]
    print(f"  {base:26} {hh:3}/{nn:<3} = {hh/nn:.0%}")

print("\nby stat type + line:")
for (base, line) in sorted(by_stat_line, key=lambda x: (x[0], x[1])):
    hh, nn = by_stat_line[(base, line)]
    print(f"  {base:26} line {line:<4} {hh:3}/{nn:<3} = {hh/nn:.0%}")

# ERA-under since implementation
era = [r for r in rows if "(ERAunder)" in (r.get("stat_type") or "")]
print(f"\n=== ERA-UNDER (since 2026-08-13 implementation): "
      f"{sum(1 for r in era if r['result']=='hit')}/{len(era)} ===")
for r in sorted(era, key=lambda x: x.get("pick_date", "")):
    mark = "✅" if r["result"] == "hit" else "❌"
    print(f"  {mark} {r.get('pick_date')} {r['player'][:20]:20} "
          f"UNDER {_f(r,'line')} Earned Runs Allowed")
if not era:
    print("  (none resolved yet — logged as (ERAunder) but not graded)")
