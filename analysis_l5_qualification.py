"""Would requiring L5 >= 4/5 improve the STAR/LOCK tiers? For each resolved
star/lock pick, reconstruct how many of the player's last 5 games (before the
pick date) cleared that pick's line, then bucket the actual hit rate by that
L5 count. Answers: what do the 3/5 (and worse) picks actually hit, and does a
4/5+ gate help — or just shrink volume?

L5 is reconstructed from statsapi game logs (not stored in pick_log). Focus is
Hitter Fantasy Score (what stars/locks are); other stat types are skipped.
"""
import json, urllib.request
from collections import defaultdict
from calibration_tracker import _sb_fetch
from daily_top_picks import _is_elite
from data.mlb_batter_stats import find_player_id, compute_hitter_fs


def _f(r, k):
    try:
        return float(r.get(k) or 0)
    except (TypeError, ValueError):
        return 0.0


def _is_lock(r):
    return "(Goblin)" in (r.get("stat_type") or "")


rows = _sb_fetch("select=player,stat_type,direction,line,p_over,pitcher_tier,park_factor,"
                 "result,pick_date&sport=eq.MLB&resolved=eq.true")
picks = []
for r in rows:
    if r.get("result") not in ("hit", "miss"):
        continue
    r["line"] = _f(r, "line"); r["park_factor"] = _f(r, "park_factor")
    r["pitcher_tier"] = r.get("pitcher_tier") or ""
    base_stat = (r.get("stat_type") or "").replace(" (Goblin)", "")
    if base_stat != "Hitter Fantasy Score":
        continue
    lock = _is_lock(r)
    star = _is_elite(r) and not lock
    if not (lock or star):
        continue
    r["tier"] = "LOCK" if lock else "STAR"
    picks.append(r)

print(f"resolved HFS star/lock picks: {len(picks)}")

# cache game logs per player
_glcache = {}
def gamelog(player):
    if player in _glcache:
        return _glcache[player]
    out = []
    try:
        pid = find_player_id(player)
        if pid:
            d = json.loads(urllib.request.urlopen(
                f"https://statsapi.mlb.com/api/v1/people/{pid}/stats?stats=gameLog&group=hitting&season=2026",
                timeout=12).read())
            for s in d.get("stats", [{}])[0].get("splits", []):
                out.append((s.get("date"), s.get("stat", {})))
    except Exception:
        pass
    _glcache[player] = out
    return out


def l5_count(player, pick_date, line):
    log = gamelog(player)
    prior = sorted([(d, st) for d, st in log if d and d < pick_date],
                   key=lambda x: x[0])[-5:]
    if len(prior) < 5:
        return None  # not enough history to judge
    cleared = 0
    for _, st in prior:
        try:
            if compute_hitter_fs(st) > line:
                cleared += 1
        except Exception:
            return None
    return cleared


buckets = defaultdict(lambda: defaultdict(lambda: [0, 0]))  # tier -> l5 -> [hits, n]
skipped = 0
for r in picks:
    c = l5_count(r["player"], r["pick_date"], r["line"])
    if c is None:
        skipped += 1
        continue
    b = buckets[r["tier"]][c]
    b[1] += 1
    b[0] += 1 if r["result"] == "hit" else 0

for tier in ("STAR", "LOCK"):
    print(f"\n=== {tier} — hit rate by L5 (games cleared, of last 5) ===")
    data = buckets[tier]
    tot_h = tot_n = 0
    for c in range(6):
        h, n = data.get(c, [0, 0])
        tot_h += h; tot_n += n
        if n:
            print(f"  L5 {c}/5: {h:3}/{n:<3} = {h/n:.0%}")
    # split at the proposed gate
    lo_h = sum(data.get(c, [0, 0])[0] for c in (0, 1, 2, 3))
    lo_n = sum(data.get(c, [0, 0])[1] for c in (0, 1, 2, 3))
    hi_h = sum(data.get(c, [0, 0])[0] for c in (4, 5))
    hi_n = sum(data.get(c, [0, 0])[1] for c in (4, 5))
    print(f"  ── L5 <=3/5 (would be CUT): {lo_h}/{lo_n} = {lo_h/lo_n:.0%}" if lo_n else "  <=3/5: none")
    print(f"  ── L5 >=4/5 (would be KEPT): {hi_h}/{hi_n} = {hi_h/hi_n:.0%}" if hi_n else "  >=4/5: none")
    print(f"  ── current all: {tot_h}/{tot_n} = {tot_h/tot_n:.0%}" if tot_n else "")
print(f"\n(skipped {skipped} picks with <5 prior games)")
