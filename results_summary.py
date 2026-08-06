"""
results_summary.py — cumulative record of what the bot has actually sent,
split by the SAME tiers the morning brief pushes (prime / star / lock /
wnba-hot / runs-watch), plus overall and recent windows.

Run via adhoc_report.yml (Supabase creds live in GitHub Secrets):
  command = results_summary.py

Tiers are reconstructed from the stored pick with the live classifiers so
the numbers match the brief exactly (prime ⊂ elite, so star = elite & not
prime — same split as morning_digest / daily_scorecard).
"""
from datetime import datetime, timezone, timedelta

from calibration_tracker import _sb_fetch
from daily_top_picks import _is_elite, _is_prime


def _f(r, k):
    try:
        return float(r.get(k) or 0)
    except (TypeError, ValueError):
        return 0.0


def _today_et():
    return (datetime.now(timezone.utc) - timedelta(hours=4)).date()


def _tier(r):
    st = r.get("stat_type") or ""
    if "(Goblin)" in st:
        return "🔒 Locks"
    if "(WNBAhot)" in st:
        return "🏀 WNBA-hot"
    if "(RunsWatch)" in st:
        return "🧪 Runs-watch"
    if _is_prime(r):
        return "🎯 Primes"
    if _is_elite(r):
        return "⭐ Stars"
    return None   # logged candidate that was never shown as a tier


def _rate(bucket):
    n = len(bucket)
    if not n:
        return "—"
    h = sum(bucket)
    return f"{h}/{n} ({h/n:.0%})"


def main():
    rows = _sb_fetch(
        "select=player,sport,stat_type,direction,line,p_over,pitcher_tier,"
        "park_factor,result,pick_date&resolved=eq.true")
    rows = [r for r in rows if r.get("result") in ("hit", "miss")]
    for r in rows:
        r["line"] = _f(r, "line")
        r["park_factor"] = _f(r, "park_factor")
        r["pitcher_tier"] = r.get("pitcher_tier") or ""

    tiers, overall, since_first = {}, [], None
    win7, win14 = [], []
    today = _today_et()
    for r in rows:
        hit = r["result"] == "hit"
        t = _tier(r)
        if t is None:
            continue                       # only count picks that were actually sent
        overall.append(hit)
        tiers.setdefault(t, []).append(hit)
        try:
            d = datetime.strptime(r["pick_date"], "%Y-%m-%d").date()
            since_first = d if since_first is None else min(since_first, d)
            age = (today - d).days
            if age <= 7:
                win7.append(hit)
            if age <= 14:
                win14.append(hit)
        except Exception:
            pass

    print("=" * 44)
    print(f"RESULTS SO FAR  (sent picks only, {len(overall)} resolved)")
    if since_first:
        print(f"since {since_first}")
    print("=" * 44)
    order = ["🎯 Primes", "⭐ Stars", "🔒 Locks", "🏀 WNBA-hot", "🧪 Runs-watch"]
    for t in order:
        if t in tiers:
            print(f"  {t:<14} {_rate(tiers[t])}")
    for t in tiers:
        if t not in order:
            print(f"  {t:<14} {_rate(tiers[t])}")
    print("-" * 44)
    print(f"  {'Overall':<14} {_rate(overall)}")
    print(f"  {'Last 14d':<14} {_rate(win14)}")
    print(f"  {'Last 7d':<14} {_rate(win7)}")


if __name__ == "__main__":
    main()
