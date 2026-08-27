"""Recent-window hit rate by sent-tier (stars/primes/locks), last 7 and 14
days — with LIVE grading of unresolved recent days.

The DB resolver lags ~a day, so a resolved-only query silently drops the most
recent (freshest, most relevant) day and makes every tier look better than
reality. This report grades any still-pending MLB pick live from box scores
(same source the scorecard uses), so the windows always run right up to
yesterday. Void/DNP picks are excluded, never counted."""
from datetime import datetime, timezone, timedelta
from calibration_tracker import _sb_fetch, _fetch_actual_mlb
from daily_top_picks import _is_elite, _is_prime


def _f(r, k):
    try:
        return float(r.get(k) or 0)
    except (TypeError, ValueError):
        return 0.0


def _base_stat(st):
    for s in (" (Goblin)", " (RunsWatch)", " (WNBAhot)", " (ERAunder)"):
        st = st.replace(s, "")
    return st


def _tier(r):
    st = r.get("stat_type") or ""
    if "(Goblin)" in st:  return "lock"
    if "(WNBAhot)" in st: return "wnba"
    if _is_prime(r):      return "prime"
    if _is_elite(r):      return "star"
    return None


# Live-grade cache so we don't refetch the same player/date box score.
_seen: dict = {}


def graded_hit(r):
    """True/False for hit/miss, or None if void/DNP/ungradeable. Uses the
    stored result when resolved; otherwise live-grades MLB from the box score."""
    res = r.get("result")
    if res in ("hit", "miss"):
        return res == "hit"
    if res == "void":
        return None
    if r.get("sport") != "MLB":
        return None                       # non-MLB pending: leave to resolver
    key = (r["player"], _base_stat(r["stat_type"]), r["pick_date"])
    if key not in _seen:
        _seen[key] = _fetch_actual_mlb(*key)
    a = _seen[key]
    if a in (None, "DNP"):
        return None
    return (a > _f(r, "line")) if r["direction"] == "OVER" else (a < _f(r, "line"))


today = (datetime.now(timezone.utc) - timedelta(hours=4)).date()
since = (today - timedelta(days=14)).strftime("%Y-%m-%d")
# NOTE: no resolved filter — we grade pending rows ourselves.
rows = _sb_fetch("select=player,sport,stat_type,direction,line,p_over,pitcher_tier,"
                 f"park_factor,result,resolved,pick_date&pick_date=gte.{since}")
for r in rows:
    r["hit"] = graded_hit(r)             # True / False / None


def rate(bucket):
    b = [r for r in bucket if r["hit"] is not None]
    if not b:
        return "—"
    h = sum(1 for r in b if r["hit"])
    return f"{h}/{len(b)} ({h/len(b):.0%})"


for label, days in (("last 7d", 7), ("last 14d", 14)):
    cut = (today - timedelta(days=days)).strftime("%Y-%m-%d")
    win = [r for r in rows if r.get("pick_date", "") >= cut]
    pend = sum(1 for r in win if r.get("result") not in ("hit", "miss", "void") and r["hit"] is not None)
    print(f"\n{label} (through {today}, {pend} live-graded):")
    for t in ("prime", "star", "lock"):
        sel = [r for r in win if _tier(r) == t]
        print(f"  {t:6}: {rate(sel)}")

print("\nSTAR picks last 14d (individual):")
stars = sorted([r for r in rows if _tier(r) == "star" and r["hit"] is not None],
               key=lambda x: x["pick_date"])
for r in stars:
    mark = "✅" if r["hit"] else "❌"
    live = "" if r.get("result") in ("hit", "miss") else " (live)"
    print(f"  {mark} {r['pick_date']} {r['player'][:20]:20} O{r['line']} p_over {r.get('p_over')}{live}")
