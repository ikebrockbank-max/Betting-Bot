"""Hit rates for the STAR and LOCK tiers — all-time and recent."""
from datetime import date, timedelta
from calibration_tracker import _sb_fetch
from daily_top_picks import _is_elite

def f(r,k):
    try: return float(r.get(k) or 0)
    except: return 0.0

rows=_sb_fetch("select=player,sport,stat_type,direction,line,p_over,result,pick_date"
               "&resolved=eq.true&result=neq.void")
rows=[r for r in rows if r.get("result") in ("hit","miss")]
for r in rows:
    r["line"]=f(r,"line")

def is_lock(r): return "(Goblin)" in (r.get("stat_type") or "")

def rate(rs):
    if not rs: return "0/0"
    h=sum(x["result"]=="hit" for x in rs)
    return f"{h}/{len(rs)} = {h/len(rs):.1%}"

def window(rs, days=None):
    if days is None: return rs
    cut=(date.today()-timedelta(days=days)).isoformat()
    return [r for r in rs if (r.get("pick_date") or "")>=cut]

stars=[r for r in rows if _is_elite(r)]
locks=[r for r in rows if is_lock(r)]

print("=== STAR tier (HFS OVER, line 4.5-6.5, p_over>=0.75) ===")
print(f"  all-time:  {rate(stars)}")
print(f"  last 14d:  {rate(window(stars,14))}")
print(f"  last 7d:   {rate(window(stars,7))}")
# first star date
if stars:
    print(f"  span: {min(r['pick_date'] for r in stars)} -> {max(r['pick_date'] for r in stars)}")

print("\n=== LOCK tier (goblin lines) ===")
print(f"  all-time:  {rate(locks)}")
print(f"  last 14d:  {rate(window(locks,14))}")
print(f"  last 7d:   {rate(window(locks,7))}")
if locks:
    print(f"  span: {min(r['pick_date'] for r in locks)} -> {max(r['pick_date'] for r in locks)}")
    print("  recent locks:")
    for r in sorted(locks,key=lambda x:x.get('pick_date',''))[-10:]:
        m="✅" if r["result"]=="hit" else "❌"
        print(f"    {m} {r['pick_date']} {r['player']:<20} O{r['line']} {r['stat_type'].replace(' (Goblin)','')}")
