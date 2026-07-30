"""Audit for real bugs in the tier/selection pipeline."""
from calibration_tracker import _sb_fetch
from daily_top_picks import _is_elite, _is_prime
def f(r,k):
    try: return float(r.get(k) or 0)
    except: return 0.0
rows=[r for r in _sb_fetch("select=player,stat_type,direction,line,p_over,pitcher_tier,"
      "park_factor,confidence,hit_rate,result,pick_date&resolved=eq.true")]
for r in rows: r["line"]=f(r,"line")
stars=[r for r in rows if _is_elite(r)]
print(f"Total resolved stars (current def): {len(stars)}")

# BUG 1: stars dropped by the 0.60 confidence floor in get_top_picks
lc=[r for r in stars if f(r,"confidence")<0.60]
print(f"\n[1] Stars with confidence<0.60 (DROPPED before _is_elite runs): {len(lc)}/{len(stars)}")
for r in lc[:6]: print(f"    {r.get('player'):<20} conf={f(r,'confidence'):.2f} p_over={f(r,'p_over'):.2f} line={r['line']}")

# BUG 2: park_factor missing/zero on picks (would wrongly exclude if not defaulted)
allhfs=[r for r in rows if r.get("stat_type")=="Hitter Fantasy Score" and r.get("direction")=="OVER"]
nopf=[r for r in allhfs if not r.get("park_factor")]
print(f"\n[2] HFS OVER picks with missing/zero park_factor: {len(nopf)}/{len(allhfs)} ({len(nopf)/max(len(allhfs),1):.0%})")

# BUG 3: prime not subset of star (should be impossible)
primes=[r for r in rows if _is_prime(r)]
notsub=[r for r in primes if not _is_elite(r)]
print(f"\n[3] Primes that are NOT stars (broken subset): {len(notsub)} (should be 0)")

# BUG 4: goblin/standard collision — same player+date+stripped-stat in both
from collections import defaultdict
seen=defaultdict(set)
for r in rows:
    st=(r.get("stat_type") or "")
    base=st.replace(" (Goblin)","")
    tag="goblin" if "(Goblin)" in st else "std"
    seen[(r.get("player"),r.get("pick_date"),base)].add(tag)
both=[k for k,v in seen.items() if len(v)>1]
print(f"\n[4] Same player+date+stat in BOTH goblin & standard (dedup key ok?): {len(both)} pairs (expected, different stat_type keys)")

# BUG 5: stars with pitcher_tier that shouldn't pass (sanity)
bad=[r for r in stars if (r.get("pitcher_tier") or "") not in ("weak","below_avg","average")]
print(f"\n[5] Stars with pitcher_tier NOT in weak/below/avg (def violation): {len(bad)} (should be 0)")
badpk=[r for r in stars if f(r,"park_factor")<1.0]
print(f"[5b] Stars with park_factor<1.0 (def violation): {len(badpk)} (should be 0)")
badpo=[r for r in stars if not (0.75<=f(r,"p_over")<=0.85)]
print(f"[5c] Stars with p_over outside 0.75-0.85 (def violation): {len(badpo)} (should be 0)")
