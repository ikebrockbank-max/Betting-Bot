"""Report every 🔒 goblin Lock pick logged so far and its result."""
from calibration_tracker import _sb_fetch

rows = _sb_fetch("select=pick_date,player,stat_type,direction,line,confidence,"
                 "p_over,hit_rate,result,actual_value"
                 "&stat_type=like.*(Goblin)*&order=pick_date.desc")
print(f"LOCK PICKS — {len(rows)} logged")
h = m = 0
for r in rows:
    res = (r.get("result") or "pending").upper()
    mark = {"HIT": "✅", "MISS": "❌", "VOID": "⬜"}.get(res, "⏳")
    if res == "HIT": h += 1
    if res == "MISS": m += 1
    print(f"  {mark} {r['pick_date']}  {r['player']:<24} OVER {r['line']:<5} "
          f"{r['stat_type']:<32} p_over={r.get('p_over')} hr={r.get('hit_rate')} "
          f"actual={r.get('actual_value')}")
if h + m:
    print(f"\nResolved: {h}/{h+m} = {h/(h+m):.1%}")
