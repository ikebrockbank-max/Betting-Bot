"""Rigorous re-test of 'does L5 recent form predict star hit rate?' with (a)
Wilson confidence bounds and (b) a LARGE-sample check on the full HFS-OVER
population at star-range lines, so the mean-reversion direction isn't judged
on 25 picks. If colder-L5 really hits >= hotter-L5 in thousands of picks, a
4/5 gate would hurt; if it's a small-sample fluke, this will expose it."""
import json, math, urllib.request
from collections import defaultdict
from calibration_tracker import _sb_fetch
from daily_top_picks import _is_elite
from data.mlb_batter_stats import find_player_id, compute_hitter_fs


def _f(r, k):
    try:
        return float(r.get(k) or 0)
    except (TypeError, ValueError):
        return 0.0


def wilson(h, n, z=1.96):
    if n == 0:
        return (0, 0)
    p = h / n
    c = (p + z*z/(2*n)) / (1 + z*z/n)
    m = z*math.sqrt((p*(1-p)+z*z/(4*n))/n) / (1 + z*z/n)
    return (max(0, c-m), min(1, c+m))


rows = _sb_fetch("select=player,stat_type,direction,line,p_over,pitcher_tier,park_factor,"
                 "result,pick_date&sport=eq.MLB&stat_type=eq.Hitter%20Fantasy%20Score"
                 "&direction=eq.OVER&resolved=eq.true")
rows = [r for r in rows if r.get("result") in ("hit", "miss")]
for r in rows:
    r["line"] = _f(r, "line"); r["park_factor"] = _f(r, "park_factor")
    r["pitcher_tier"] = r.get("pitcher_tier") or ""
print(f"resolved HFS OVER picks: {len(rows)}")

_gl = {}
def gamelog(player):
    if player in _gl:
        return _gl[player]
    out = []
    try:
        pid = find_player_id(player)
        if pid:
            d = json.loads(urllib.request.urlopen(
                f"https://statsapi.mlb.com/api/v1/people/{pid}/stats?stats=gameLog&group=hitting&season=2026",
                timeout=12).read())
            out = [(s.get("date"), s.get("stat", {})) for s in d.get("stats", [{}])[0].get("splits", [])]
    except Exception:
        pass
    _gl[player] = out
    return out


def l5(player, pick_date, line):
    prior = sorted([(d, st) for d, st in gamelog(player) if d and d < pick_date],
                   key=lambda x: x[0])[-5:]
    if len(prior) < 5:
        return None
    try:
        return sum(1 for _, st in prior if compute_hitter_fs(st) > line)
    except Exception:
        return None


def show(title, subset):
    print(f"\n=== {title} (n={len(subset)}) ===")
    b = defaultdict(lambda: [0, 0])
    for r in subset:
        c = l5(r["player"], r["pick_date"], r["line"])
        if c is None:
            continue
        b[c][1] += 1; b[c][0] += 1 if r["result"] == "hit" else 0
    for c in range(6):
        h, n = b.get(c, [0, 0])
        if n:
            lo, hi = wilson(h, n)
            print(f"  L5 {c}/5: {h:4}/{n:<4} = {h/n:.0%}  [95% {lo:.0%}-{hi:.0%}]")
    lo_h = sum(b.get(c, [0, 0])[0] for c in (0, 1, 2, 3)); lo_n = sum(b.get(c, [0, 0])[1] for c in (0, 1, 2, 3))
    hi_h = sum(b.get(c, [0, 0])[0] for c in (4, 5)); hi_n = sum(b.get(c, [0, 0])[1] for c in (4, 5))
    if lo_n and hi_n:
        print(f"  ≤3/5 (would CUT): {lo_h}/{lo_n} = {lo_h/lo_n:.0%}   |   ≥4/5 (would KEEP): {hi_h}/{hi_n} = {hi_h/hi_n:.0%}")


# (1) star tier (elite, incl primes) — the actual sent picks
show("STAR TIER (elite)", [r for r in rows if _is_elite(r)])
# (2) LARGE sample: all HFS OVER at star-range lines (5.5-6.5), soft-ish pitchers
big = [r for r in rows if 5.5 <= r["line"] <= 6.5]
show("ALL HFS OVER, line 5.5-6.5 (large sample)", big)
# (3) even broader: every HFS OVER regardless of line
show("ALL HFS OVER (any line, largest sample)", rows)
