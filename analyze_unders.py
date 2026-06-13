"""
analyze_unders.py — Check UNDER pick performance by stat type and hit_rate bucket.
Run via GHA where SUPABASE_ANON_KEY is available.
"""
import os, json, urllib.request, re

SB_URL = os.getenv("SUPABASE_URL", "https://gggozciyvjeqjnmufigp.supabase.co")
SB_KEY = os.getenv("SUPABASE_ANON_KEY", "")
TABLE  = "pick_log"

def fetch_all(params: str) -> list[dict]:
    headers = {
        "apikey": SB_KEY,
        "Authorization": f"Bearer {SB_KEY}",
        "Accept": "application/json",
    }
    clean = re.sub(r"&?limit=\d+", "", params).lstrip("&")
    rows, offset, page = [], 0, 1000
    while True:
        req = urllib.request.Request(
            f"{SB_URL}/rest/v1/{TABLE}?{clean}&limit={page}&offset={offset}",
            headers=headers,
        )
        batch = json.loads(urllib.request.urlopen(req, timeout=20).read())
        if not isinstance(batch, list): break
        rows.extend(batch)
        if len(batch) < page: break
        offset += page
    return rows

def pct(h, t):
    return f"{h/t:.0%} ({h}/{t})" if t else "—"

print("Fetching all resolved UNDER picks...")
picks = fetch_all("select=*&resolved=eq.true&direction=eq.UNDER")
print(f"Total resolved UNDER picks: {len(picks)}")

bet   = [p for p in picks if p.get("was_qualified")]
watch = [p for p in picks if not p.get("was_qualified")]
bet_h = sum(1 for p in bet if p.get("result") == "hit")

print(f"Bet UNDER picks: {len(bet)}  hit rate: {pct(bet_h, len(bet))}")

# ── By stat type ──────────────────────────────────────────────────────────────
print("\n" + "="*65)
print("  UNDER BET PICKS — by stat type (n≥3)")
print("="*65)
from collections import defaultdict
by_stat = defaultdict(lambda: {"hits": 0, "total": 0, "sport": ""})
for p in bet:
    st = p.get("stat_type", "?")
    by_stat[st]["hits"]  += (1 if p.get("result") == "hit" else 0)
    by_stat[st]["total"] += 1
    by_stat[st]["sport"]  = p.get("sport", "")

ranked = [(st, d["hits"]/d["total"], d["hits"], d["total"], d["sport"])
          for st, d in by_stat.items() if d["total"] >= 3]
ranked.sort(key=lambda x: -x[1])
print(f"  {'Stat type':<28} {'Sport':<6} {'Hit rate':<14} N")
print(f"  {'-'*60}")
for st, rate, h, t, sport in ranked:
    flag = "✅" if rate >= 0.60 else ("⚠️" if rate >= 0.50 else "❌")
    print(f"  {flag} {st:<26} {sport:<6} {pct(h,t):<16} {t}")

# ── High-signal UNDER picks: hit_rate ≥ 0.75 ──────────────────────────────
print("\n" + "="*65)
print("  HIGH-SIGNAL UNDER BET PICKS — historical hit_rate ≥ 75%")
print("="*65)
high_sig = [p for p in bet if (p.get("hit_rate") or 0) >= 0.75]
high_hits = sum(1 for p in high_sig if p.get("result") == "hit")
print(f"  n={len(high_sig)}  actual hit rate: {pct(high_hits, len(high_sig))}")

# Break by stat type
by_stat2 = defaultdict(lambda: {"hits": 0, "total": 0, "sport": ""})
for p in high_sig:
    st = p.get("stat_type", "?")
    by_stat2[st]["hits"]  += (1 if p.get("result") == "hit" else 0)
    by_stat2[st]["total"] += 1
    by_stat2[st]["sport"]  = p.get("sport", "")
ranked2 = [(st, d["hits"]/d["total"], d["hits"], d["total"], d["sport"])
           for st, d in by_stat2.items() if d["total"] >= 2]
ranked2.sort(key=lambda x: -x[1])
print(f"  {'Stat type':<28} {'Sport':<6} {'Actual hit%':<14} N")
print(f"  {'-'*60}")
for st, rate, h, t, sport in ranked2:
    flag = "✅" if rate >= 0.60 else ("⚠️" if rate >= 0.50 else "❌")
    print(f"  {flag} {st:<26} {sport:<6} {pct(h,t):<16} {t}")

# ── UNDER picks by hit_rate bucket ────────────────────────────────────────
print("\n" + "="*65)
print("  UNDER BET — does historical hit_rate predict actual outcome?")
print("="*65)
print(f"  {'HR bucket':<12} {'Actual hit%':<16} N")
print(f"  {'-'*40}")
for lo, hi in [(0.60,0.65),(0.65,0.70),(0.70,0.75),(0.75,0.80),(0.80,1.0)]:
    b = [p for p in bet if lo <= (p.get("hit_rate") or 0) < hi]
    if not b: continue
    h = sum(1 for p in b if p.get("result") == "hit")
    flag = "✅" if h/len(b) >= 0.60 else ("⚠️" if h/len(b) >= 0.50 else "❌")
    print(f"  {lo:.0%}-{hi:.0%}     {pct(h,len(b)):<18} {flag}")

# ── Pitcher-specific UNDERs ────────────────────────────────────────────────
print("\n" + "="*65)
print("  PITCHER STAT UNDERS specifically")
print("="*65)
pitcher_stats = {"Pitcher Fantasy Score","Pitcher Strikeouts","Strikeouts",
                 "Pitching Outs","Earned Runs Allowed","Hits Allowed",
                 "Walks Allowed","Pitches Thrown"}
p_under = [p for p in bet if p.get("stat_type","") in pitcher_stats]
p_hits  = sum(1 for p in p_under if p.get("result") == "hit")
print(f"  All pitcher UNDER bet picks: {pct(p_hits, len(p_under))}")
for st in pitcher_stats:
    b = [p for p in bet if p.get("stat_type","") == st]
    if not b: continue
    h = sum(1 for p in b if p.get("result") == "hit")
    flag = "✅" if h/len(b) >= 0.60 else ("⚠️" if h/len(b) >= 0.50 else "❌")
    print(f"  {flag} {st:<30} {pct(h,len(b))}")

print("\nDone.")
