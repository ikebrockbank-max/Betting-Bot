"""
Combined morning brief — ONE ntfy push with everything:
  • Yesterday's scorecard (how the tiers did)
  • Today's ⭐ star picks
  • Today's 🔒 goblin locks

Replaces the two separate morning pushes (scorecard + picks) that left
the user seeing only a bare "Scorecard" notification with no picks. Runs
at 9 AM local from morning_digest.yml. Also logs today's picks to the
calibration tracker so tomorrow's scorecard can grade them.
"""
import os
import sys
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta

from daily_scorecard import build_scorecard
from daily_top_picks import (get_top_picks, _find_locks, _find_runs_watch,
                             _is_elite, _is_prime, _predicted_rate)

# Sent-today marker (persisted across runs via actions/cache), so the first
# retry slot that succeeds sends and later slots exit — same pattern as the
# picks workflow. Best-effort: a cache miss risks a duplicate, never a miss.
_MARKER = Path("logs/.digest_sent.json")


def _today_et():
    return (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%Y-%m-%d")


def _already_sent():
    try:
        return json.loads(_MARKER.read_text()).get("date") == _today_et()
    except Exception:
        return False


def _mark_sent():
    try:
        _MARKER.parent.mkdir(exist_ok=True)
        _MARKER.write_text(json.dumps({"date": _today_et()}))
    except Exception as e:
        print(f"[digest] could not write marker: {e}")


def build_digest():
    lines = []

    # 1) Yesterday's scorecard
    try:
        lines.append(build_scorecard())          # defaults to yesterday
    except Exception as e:
        lines.append(f"📊 Scorecard unavailable ({e})")

    # 2) Today's picks + locks. Only park>=1.0 high-signal picks are sent —
    # park just-below-1.0 (0.98) backtested at 33%, so those are dropped
    # entirely, not shown. Each pick still displays its park + predicted
    # rate for transparency.
    picks_by_sport, fetch_failures = get_top_picks(["MLB"], n=6)
    mlb = picks_by_sport.get("MLB", [])
    primes = [p for p in mlb if _is_prime(p)]
    stars = [p for p in mlb if _is_elite(p) and not _is_prime(p)]
    locks = _find_locks(n=3)
    runs_watch = _find_runs_watch(n=2)

    def pk(p):
        return float(p.get("park_factor", 0) or 0)

    lines.append("")
    lines.append(f"TODAY'S PICKS {_today_et()[5:]}")
    for p in primes:
        lines.append(f"🎯 {p['player']} OVER {p['line']} {p['stat_type']} "
                     f"(prime) — park {pk(p):.2f}, {_predicted_rate(p)}")
    for p in stars:
        lines.append(f"⭐ {p['player']} OVER {p['line']} {p['stat_type']} "
                     f"— park {pk(p):.2f}, {_predicted_rate(p)}")
    if not primes and not stars:
        lines.append("(no high-signal picks cleared the bar today)")

    lines.append("")
    lines.append("🔒 LOCKS (safer, goblin lines)")
    if locks:
        for p in locks:
            lines.append(f"🔒 {p['player']} OVER {p['line']} "
                         f"{p['stat_type'].replace(' (Goblin)','')}")
    else:
        lines.append("(no locks today)")

    if runs_watch:
        lines.append("")
        lines.append("🧪 RUNS WATCH (experimental, unproven n=31 ~71%):")
        for p in runs_watch:
            lines.append(f"🧪 {p['player']} UNDER {p['line']} "
                         f"{p['stat_type'].replace(' (RunsWatch)','')}")

    if fetch_failures:
        lines.append(f"\n⚠️ fetch issue: {', '.join(fetch_failures)}")

    # PrizePicks moves lines through the day — a pick flagged at 4.5 this
    # morning can be 5 by game time, which changes the odds. Always confirm
    # the live line in the app before betting.
    hhmm = datetime.now(timezone.utc).strftime("%H:%M UTC")
    lines.append(f"\n⏰ lines as of {hhmm} — confirm live line in app")

    body = "\n".join(lines)

    # Log today's picks (+locks) so tomorrow's scorecard can grade them.
    try:
        from calibration_tracker import log_pick
        for p in mlb:
            log_pick(p)
        for p in locks:
            log_pick(p)
        for p in runs_watch:
            log_pick(p)
    except Exception as e:
        print(f"[digest] calibration logging failed: {e}")

    return body, len(primes) + len(stars), len(locks)


def main():
    force = os.getenv("FORCE_RESEND", "").lower() == "true"
    if _already_sent() and not force:
        print("Digest already sent today — retry slot, exiting.")
        return
    body, n_stars, n_locks = build_digest()
    print(body)
    # Only send + mark if we actually got today's picks (a blocked/empty scan
    # produces the no-picks line — don't burn the marker so a later slot retries)
    got_picks = n_stars > 0 or n_locks > 0
    try:
        from notify import send_push
        title = f"🎯 Daily Brief — {n_stars} picks, {n_locks} locks"
        if send_push(body, title=title):
            print("\n[pushed via ntfy]")
            if got_picks:
                _mark_sent()
        else:
            print("\n[push FAILED]")
    except Exception as e:
        print(f"\n[push failed: {e}]")


if __name__ == "__main__":
    main()
