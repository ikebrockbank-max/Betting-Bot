"""
Daily scorecard — grades yesterday's logged picks on the corrected
PrizePicks scale and pushes a concise summary via ntfy.

Runs at 8 AM local (14:00 UTC) from daily_scorecard.yml, AFTER all of
yesterday's games are final. Grades live from box scores (via
_fetch_actual_mlb), so it doesn't depend on the resolver having run yet.

  arg (optional) = date YYYY-MM-DD (default: yesterday ET)
"""
import sys
from datetime import datetime, timezone, timedelta

from calibration_tracker import _sb_fetch, _fetch_actual_mlb
from parlay_builder import _passes_direction_gate
from daily_top_picks import _is_elite, _is_prime


def _f(r, k):
    try:
        return float(r.get(k) or 0)
    except (TypeError, ValueError):
        return 0.0


def _is_lock(r):
    return "(Goblin)" in (r.get("stat_type") or "")


def build_scorecard(date=None) -> str:
    """Grade `date` (default yesterday ET) and return the scorecard body
    text. No push — the caller decides how to deliver it (used standalone
    and folded into the combined morning digest)."""
    if date is None:
        date = (datetime.now(timezone.utc) - timedelta(hours=4)
                - timedelta(days=1)).strftime("%Y-%m-%d")
    # hit_rate/pitcher_tier/edge_pct are required by _passes_direction_gate —
    # omitting them silently fails the gate for most hitter OVERs and craters
    # the 'gate' bucket (caught 2026-07-24: reported 4/6 instead of 38/74).
    # park_factor is REQUIRED by _is_elite (the 79% star def). It was added to
    # _is_elite later but not to this SELECT, so every pick had park=0 and the
    # STAR bucket silently counted ZERO — 7/31 & 8/1 each had 3 stars (all hit)
    # reported as 0. Same class of bug as the earlier missing-hit_rate one.
    rows = _sb_fetch(f"select=player,sport,stat_type,direction,line,confidence,"
                     f"p_over,hit_rate,pitcher_tier,edge_pct,park_factor,result,pick_date"
                     f"&pick_date=eq.{date}")
    for r in rows:
        r["line"] = _f(r, "line")
        r["hit_rate"] = _f(r, "hit_rate")
        r["edge_pct"] = _f(r, "edge_pct")
        r["park_factor"] = _f(r, "park_factor")
        r["pitcher_tier"] = r.get("pitcher_tier") or ""

    def grade(r):
        if r.get("sport") == "MLB":
            look = r["stat_type"].replace(" (Goblin)", "")
            a = _fetch_actual_mlb(r["player"], look, date)
            if a in (None, "DNP"):
                return None
            return (a > r["line"]) if r["direction"] == "OVER" else (a < r["line"])
        if r.get("result") in ("hit", "miss"):
            return r["result"] == "hit"
        return None

    # STAR must match EXACTLY what the morning brief displayed under ⭐:
    # elite-but-not-prime. A prime is also _is_elite, so counting bare
    # _is_elite double-billed primes as stars (user saw 1 ⭐ but scorecard
    # said 2). Primes get their own 🎯 line; stars exclude them, same split
    # as morning_digest.build_digest.
    tiers = {"PRIME": [], "STAR": [], "LOCK": [], "gate": [], "all": []}
    star_detail = []
    for r in rows:
        hit = grade(r)
        if hit is None:
            continue
        tiers["all"].append(hit)
        if _is_prime(r):
            tiers["PRIME"].append(hit)
            star_detail.append((r["player"].split()[-1], hit))
        elif _is_elite(r):
            tiers["STAR"].append(hit)
            star_detail.append((r["player"].split()[-1], hit))
        if _is_lock(r):
            tiers["LOCK"].append(hit)
        if r.get("sport") == "MLB" and not _is_lock(r) and _passes_direction_gate(r):
            tiers["gate"].append(hit)

    def line(label, b):
        if not b:
            return None
        return f"{label}: {sum(b)}/{len(b)} ({sum(b)/len(b):.0%})"

    parts = [f"📊 Scorecard {date[5:]}"]
    for k, lbl in (("PRIME", "🎯 Primes"), ("STAR", "⭐ Stars"), ("LOCK", "🔒 Locks"),
                   ("gate", "Gate"), ("all", "All")):
        ln = line(lbl, tiers[k])
        if ln:
            parts.append(ln)
    if star_detail:
        hits = [n for n, h in star_detail if h]
        misses = [n for n, h in star_detail if not h]
        if hits:
            parts.append("✅ " + ", ".join(hits))
        if misses:
            parts.append("❌ " + ", ".join(misses))
    return "\n".join(parts)


def run(date=None):
    body = build_scorecard(date)
    print(body)
    label = (date or "")[5:] or "yesterday"
    try:
        from notify import send_push
        send_push(body, title=f"📊 Daily Scorecard {label}")
        print("\n[pushed via ntfy]")
    except Exception as e:
        print(f"\n[push failed: {e}]")


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else None)
