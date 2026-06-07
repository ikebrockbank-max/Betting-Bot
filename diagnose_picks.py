"""
diagnose_picks.py — Deep dive into why picks are underperforming.

Pulls all resolved picks from Supabase and breaks down hit/miss patterns
across every dimension: sport, stat type, direction, edge size, hit_rate bucket,
confidence bucket, OVER vs UNDER split, and individual player accuracy.

Run: python3 diagnose_picks.py
"""

import json
import os
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from collections import defaultdict

_SB_URL = os.getenv("SUPABASE_URL", "https://gggozciyvjeqjnmufigp.supabase.co")
_SB_KEY = os.getenv("SUPABASE_ANON_KEY", "")


def _fetch(params: str) -> list[dict]:
    headers = {
        "apikey": _SB_KEY,
        "Authorization": f"Bearer {_SB_KEY}",
        "Accept": "application/json",
    }
    req = urllib.request.Request(
        f"{_SB_URL}/rest/v1/pick_log?{params}",
        headers=headers,
    )
    resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
    return resp if isinstance(resp, list) else []


def pct(hits, total):
    if total == 0: return "—"
    return f"{hits/total:.0%} ({hits}/{total})"


def bar(rate, width=20):
    if rate is None: return ""
    filled = int(rate * width)
    flag = "✅" if rate >= 0.55 else ("⚠️" if rate >= 0.50 else "❌")
    return f"{flag} {'█' * filled}{'░' * (width - filled)} {rate:.0%}"


def section(title):
    print(f"\n{'='*65}")
    print(f"  {title}")
    print(f"{'='*65}")


def subsection(title):
    print(f"\n  ── {title} ──")


def analyze():
    print("Fetching all resolved picks from Supabase...")
    picks = _fetch("select=*&resolved=eq.true&limit=5000&order=pick_date.desc")
    if not picks:
        print("No resolved picks found.")
        return

    total = len(picks)
    hits = sum(1 for p in picks if p.get("result") == "hit")
    bet = [p for p in picks if p.get("was_qualified")]
    watched = [p for p in picks if not p.get("was_qualified")]
    bet_hits = sum(1 for p in bet if p.get("result") == "hit")

    print(f"\n{'='*65}")
    print(f"  DEEP DIVE DIAGNOSTIC — {total} resolved picks")
    print(f"  {len(bet)} BET  |  {len(watched)} watched")
    print(f"  Overall hit rate:    {pct(hits, total)}")
    print(f"  Bet picks hit rate:  {pct(bet_hits, len(bet))}")
    print(f"{'='*65}")

    # ── 1. OVER vs UNDER split ────────────────────────────────────────────
    section("1. OVER vs UNDER — Is the direction wrong?")
    for direction in ["OVER", "UNDER"]:
        d = [p for p in picks if p.get("direction") == direction]
        h = sum(1 for p in d if p.get("result") == "hit")
        db = [p for p in d if p.get("was_qualified")]
        hb = sum(1 for p in db if p.get("result") == "hit")
        rate = hb / len(db) if db else 0
        print(f"  {direction:<6}  all: {pct(h, len(d))}   bet: {pct(hb, len(db))}  {bar(rate)}")

    # ── 2. Hit rate accuracy ───────────────────────────────────────────────
    section("2. Historical hit_rate vs Actual — Does past predict future?")
    print(f"  {'HR bucket':<12} {'Actual hit%':<15} {'N bet':<8} {'Gap'}")
    print(f"  {'-'*50}")
    for lo, hi in [(0.60, 0.65), (0.65, 0.70), (0.70, 0.75), (0.75, 0.80), (0.80, 1.0)]:
        b = [p for p in bet if lo <= (p.get("hit_rate") or 0) < hi]
        if len(b) < 3: continue
        h = sum(1 for p in b if p.get("result") == "hit")
        actual = h / len(b)
        midpoint = (lo + hi) / 2
        gap = actual - midpoint
        flag = "✅" if gap > -0.05 else ("⚠️" if gap > -0.12 else "❌")
        print(f"  {lo:.0%}-{hi:.0%}     {actual:.0%} actual    n={len(b):<5}  "
              f"{gap:+.0%} vs expected  {flag}")

    # ── 3. Edge size accuracy ─────────────────────────────────────────────
    section("3. Edge size vs Actual — Does a bigger gap predict better?")
    print(f"  {'Edge bucket':<14} {'Actual hit%':<15} {'N bet':<8}")
    print(f"  {'-'*45}")
    for lo, hi in [(0.08, 0.15), (0.15, 0.20), (0.20, 0.25),
                   (0.25, 0.30), (0.30, 0.40), (0.40, 0.60), (0.60, 1.0)]:
        b = [p for p in bet if lo <= (p.get("edge_pct") or 0) < hi]
        if len(b) < 3: continue
        h = sum(1 for p in b if p.get("result") == "hit")
        rate = h / len(b)
        print(f"  {lo:.0%}–{hi:.0%}       {pct(h, len(b)):<18}  {bar(rate)}")

    # ── 4. Sport breakdown ────────────────────────────────────────────────
    section("4. Sport breakdown")
    for sport in ["MLB", "WNBA", "NBA"]:
        s = [p for p in bet if p.get("sport") == sport]
        if len(s) < 3: continue
        h = sum(1 for p in s if p.get("result") == "hit")
        rate = h / len(s)
        print(f"  {sport}:  {pct(h, len(s))}  {bar(rate)}")

        # OVER vs UNDER within sport
        for d in ["OVER", "UNDER"]:
            sd = [p for p in s if p.get("direction") == d]
            if len(sd) < 3: continue
            sh = sum(1 for p in sd if p.get("result") == "hit")
            sr = sh / len(sd)
            print(f"    {d}:  {pct(sh, len(sd))}  {bar(sr)}")

    # ── 5. Stat type full breakdown ───────────────────────────────────────
    section("5. Every stat type — sorted by hit rate")
    by_stat = defaultdict(lambda: {"hits": 0, "total": 0, "sport": ""})
    for p in bet:
        st = p.get("stat_type", "?")
        by_stat[st]["hits"] += (1 if p.get("result") == "hit" else 0)
        by_stat[st]["total"] += 1
        by_stat[st]["sport"] = p.get("sport", "")

    ranked = [(st, d["hits"] / d["total"], d["total"], d["sport"])
              for st, d in by_stat.items() if d["total"] >= 5]
    ranked.sort(key=lambda x: -x[1])
    print(f"  {'Stat type':<28} {'Sport':<6} {'Hit rate':<12} {'N'}")
    print(f"  {'-'*58}")
    for st, rate, n, sport in ranked:
        flag = "✅" if rate >= 0.55 else ("⚠️" if rate >= 0.50 else "❌")
        print(f"  {flag} {st:<26} {sport:<6} {rate:.0%}           {n}")

    # ── 6. Confidence score vs reality ────────────────────────────────────
    section("6. Model confidence vs actual — Is ANY bucket calibrated?")
    print(f"  {'Bucket':<10} {'Model said':<12} {'Actually hit':<15} {'Gap':<10} N")
    print(f"  {'-'*55}")
    for lo, hi in [(50,60),(60,65),(65,70),(70,75),(75,80),(80,85),(85,100)]:
        b = [p for p in picks if lo <= (p.get("conf_pct") or 0) < hi]
        bb = [p for p in b if p.get("was_qualified")]
        if len(b) < 5: continue
        h = sum(1 for p in b if p.get("result") == "hit")
        hb = sum(1 for p in bb if p.get("result") == "hit")
        rate = h / len(b)
        gap = rate - (lo+hi)/2/100
        flag = "✅" if abs(gap) < 0.05 else "🔴"
        print(f"  {lo}-{hi}%    {(lo+hi)/2:.0f}%        {pct(h,len(b)):<18} {gap:+.0%}    "
              f"{flag}  (bet: {pct(hb,len(bb))})")

    # ── 7. Are we picking the right direction? ───────────────────────────
    section("7. Direction accuracy by stat type")
    print("  Checking: when we say OVER, does the player actually go OVER?")
    by_stat_dir = defaultdict(lambda: {"over_right": 0, "under_right": 0,
                                        "over_wrong": 0, "under_wrong": 0})
    for p in bet:
        st = p.get("stat_type", "?")
        d = p.get("direction", "?")
        hit = p.get("result") == "hit"
        miss = p.get("result") == "miss"
        if d == "OVER":
            if hit: by_stat_dir[st]["over_right"] += 1
            elif miss: by_stat_dir[st]["over_wrong"] += 1
        elif d == "UNDER":
            if hit: by_stat_dir[st]["under_right"] += 1
            elif miss: by_stat_dir[st]["under_wrong"] += 1

    print(f"  {'Stat type':<28} {'OVER hit%':<12} {'UNDER hit%'}")
    print(f"  {'-'*55}")
    for st, d in sorted(by_stat_dir.items(),
                        key=lambda x: -(x[1]["over_right"]+x[1]["under_right"]+
                                        x[1]["over_wrong"]+x[1]["under_wrong"])):
        ot = d["over_right"] + d["over_wrong"]
        ut = d["under_right"] + d["under_wrong"]
        if ot + ut < 5: continue
        op = pct(d["over_right"], ot) if ot > 0 else "—"
        up = pct(d["under_right"], ut) if ut > 0 else "—"
        print(f"  {st:<28} {op:<14} {up}")

    # ── 8. Model vs reality on p_hit ────────────────────────────────────
    section("8. Model p_hit accuracy — Is the probability engine trustworthy?")
    print("  p_over/p_under from distribution model vs actual outcome")
    has_model = [p for p in bet if p.get("p_over") is not None]
    no_model  = [p for p in bet if p.get("p_over") is None]
    hm = sum(1 for p in has_model if p.get("result") == "hit")
    hn = sum(1 for p in no_model  if p.get("result") == "hit")
    print(f"  Picks WITH model p_hit:     {pct(hm, len(has_model))}")
    print(f"  Picks WITHOUT model p_hit:  {pct(hn, len(no_model))}")

    for lo, hi in [(0.60, 0.70), (0.70, 0.80), (0.80, 0.90), (0.90, 1.0)]:
        b_over  = [p for p in bet if p.get("direction")=="OVER"
                   and lo <= (p.get("p_over") or 0) < hi]
        b_under = [p for p in bet if p.get("direction")=="UNDER"
                   and lo <= (p.get("p_under") or 0) < hi]
        b = b_over + b_under
        if len(b) < 3: continue
        h = sum(1 for p in b if p.get("result") == "hit")
        rate = h / len(b)
        mid = (lo + hi) / 2
        print(f"  p_hit {lo:.0%}-{hi:.0%}:  model said {mid:.0%}, actually {rate:.0%}  "
              f"(n={len(b)})  {bar(rate)}")

    # ── 9. Is the hit_rate predictor itself valid? ────────────────────────
    section("9. Are players' historical hit rates predictive AT ALL?")
    print("  Comparing hit_rate (historical) vs whether they hit today")
    # Bucket by hit_rate and see if actual results track
    for lo, hi in [(0.50,0.60),(0.60,0.65),(0.65,0.70),(0.70,0.75),(0.75,1.0)]:
        b = [p for p in picks if lo <= (p.get("hit_rate") or 0) < hi]
        if len(b) < 5: continue
        h = sum(1 for p in b if p.get("result") == "hit")
        rate = h / len(b)
        gap = rate - (lo + hi) / 2
        flag = "✅" if gap > -0.05 else ("⚠️" if gap > -0.10 else "❌ BAD SIGNAL")
        print(f"  HR {lo:.0%}-{hi:.0%}:  actual {rate:.0%}  (n={len(b)})  {gap:+.0%}  {flag}")

    # ── 10. Worst individual picks by miss margin ────────────────────────
    section("10. Biggest misses — where did the model go most wrong?")
    print("  (picks where model was most confident but missed)")
    misses = [p for p in bet if p.get("result") == "miss" and p.get("conf_pct")]
    misses.sort(key=lambda x: -(x.get("conf_pct") or 0))
    print(f"  {'Player':<24} {'Stat':<22} {'Dir':<6} {'Line':<6} "
          f"{'Actual':<8} {'Model%':<8} {'HR'}")
    print(f"  {'-'*80}")
    for p in misses[:20]:
        actual = p.get("actual_value")
        actual_str = f"{actual}" if actual is not None else "?"
        print(f"  {p.get('player','?'):<24} {p.get('stat_type','?'):<22} "
              f"{p.get('direction','?'):<6} {p.get('line','?'):<6} "
              f"{actual_str:<8} {p.get('conf_pct','?')}%     "
              f"{p.get('hit_rate',0):.0%}")

    # ── 11. Best individual picks ─────────────────────────────────────────
    subsection("Best hit players (5+ picks, sorted by hit rate)")
    by_player = defaultdict(lambda: {"hits": 0, "total": 0})
    for p in bet:
        pl = p.get("player", "?")
        by_player[pl]["hits"] += (1 if p.get("result") == "hit" else 0)
        by_player[pl]["total"] += 1
    player_ranked = [(pl, d["hits"]/d["total"], d["hits"], d["total"])
                     for pl, d in by_player.items() if d["total"] >= 3]
    player_ranked.sort(key=lambda x: -x[1])
    print(f"  {'Player':<26} {'Hit rate':<12} {'N'}")
    for pl, rate, h, n in player_ranked[:15]:
        flag = "✅" if rate >= 0.65 else ("⚠️" if rate >= 0.55 else "❌")
        print(f"  {flag} {pl:<24} {rate:.0%}   ({h}/{n})")

    # ── 12. Date trend — getting better or worse? ────────────────────────
    section("12. Performance trend by date — improving over time?")
    by_date = defaultdict(lambda: {"hits": 0, "total": 0})
    for p in bet:
        d = p.get("pick_date", "?")[:10]
        by_date[d]["total"] += 1
        by_date[d]["hits"] += (1 if p.get("result") == "hit" else 0)
    for date in sorted(by_date.keys()):
        d = by_date[date]
        if d["total"] < 3: continue
        rate = d["hits"] / d["total"]
        print(f"  {date}:  {pct(d['hits'], d['total']):<18} {bar(rate, 15)}")

    # ── 13. Key hypothesis tests ─────────────────────────────────────────
    section("13. Key hypotheses")

    # Are we systematically picking OVERs when we should be picking UNDERs?
    overs = [p for p in bet if p.get("direction") == "OVER"]
    unders = [p for p in bet if p.get("direction") == "UNDER"]
    oh = sum(1 for p in overs if p.get("result") == "hit")
    uh = sum(1 for p in unders if p.get("result") == "hit")
    print(f"  OVER picks:   {pct(oh, len(overs))}")
    print(f"  UNDER picks:  {pct(uh, len(unders))}")
    if len(overs) > 0 and len(unders) > 0:
        over_rate = oh / len(overs)
        under_rate = uh / len(unders)
        if over_rate < 0.50 and under_rate > 0.55:
            print("  ⚠️  OVERS are severely underperforming — model may be biased toward OVER")
        elif under_rate < 0.50 and over_rate > 0.55:
            print("  ⚠️  UNDERS are severely underperforming")
        elif over_rate < 0.50 and under_rate < 0.50:
            print("  ❌  BOTH directions underperforming — directional bias is not the problem")

    # Is the line set against us (PrizePicks has edge)?
    avg_edge = sum(p.get("edge_pct", 0) or 0 for p in bet) / len(bet) if bet else 0
    print(f"\n  Average edge in bet picks: {avg_edge:.1%}")
    if avg_edge < 0.15:
        print("  ⚠️  Very small average edge — we may be picking right at the line")

    # Projection error — are we systematically over or under-projecting?
    proj_errors = [p.get("proj_error") for p in picks if p.get("proj_error") is not None]
    if proj_errors:
        avg_err = sum(proj_errors) / len(proj_errors)
        mae = sum(abs(e) for e in proj_errors) / len(proj_errors)
        print(f"\n  Projection bias: {avg_err:+.2f} ({'over' if avg_err > 0 else 'under'}-projecting)")
        print(f"  Projection MAE:  ±{mae:.2f}")
        if avg_err > 0.5:
            print("  ⚠️  Model systematically OVER-projects — actual results are lower than expected")
            print("       → OVER picks are harder to hit because line is above true expectation")
        elif avg_err < -0.5:
            print("  ⚠️  Model systematically UNDER-projects — actual results are higher than expected")

    print(f"\n{'='*65}\n")


if __name__ == "__main__":
    analyze()
