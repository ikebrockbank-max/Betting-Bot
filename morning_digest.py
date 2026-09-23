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
                             _find_wnba_hot, _find_era_under,
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


# ── Atomic once-a-day send lock (Supabase) ─────────────────────────────────────
# The actions/cache marker only saves at job END, so two delayed cron slots that
# fire minutes apart each see "not sent" and BOTH push — the 2-notifications bug.
# Supabase is immediately consistent: we INSERT a per-day sentinel row into
# pick_log, whose unique key (pick_date, player, stat_type) lets exactly ONE run
# win; concurrent runs get HTTP 409 and stand down. Fails OPEN if Supabase is
# unavailable so a local run still sends.
_SB_URL = os.getenv("SUPABASE_URL", "https://gggozciyvjeqjnmufigp.supabase.co")
_SB_KEY = os.getenv("SUPABASE_ANON_KEY", "")
_CLAIM = {"player": "__DIGEST_SENT__", "sport": "MARKER", "stat_type": "__marker__"}


def _sb_headers():
    return {"apikey": _SB_KEY, "Authorization": f"Bearer {_SB_KEY}",
            "Content-Type": "application/json"}


def _claim_send_slot() -> bool:
    """True if THIS run won today's single send slot (safe to push), False if
    another slot already claimed it. Plain INSERT (no on_conflict) so a duplicate
    returns 409."""
    if not _SB_KEY:
        return True
    import urllib.request as _r, urllib.error as _e
    row = {**_CLAIM, "pick_date": _today_et(), "line": 0.0, "direction": "",
           "confidence": 0, "resolved": True, "was_qualified": False}
    req = _r.Request(f"{_SB_URL}/rest/v1/pick_log", data=json.dumps(row).encode(),
                     headers={**_sb_headers(), "Prefer": "return=minimal"}, method="POST")
    try:
        _r.urlopen(req, timeout=10)
        return True
    except _e.HTTPError as ex:
        if ex.code == 409:
            return False
        print(f"[digest] claim HTTP {ex.code}: {ex.read().decode()[:150]} — sending anyway")
        return True
    except Exception as ex:
        print(f"[digest] claim failed ({ex}) — sending anyway")
        return True


def _release_send_slot():
    """Delete today's sentinel so a later slot can retry — used only when the
    push itself fails after we'd already claimed."""
    if not _SB_KEY:
        return
    import urllib.request as _r
    params = (f"pick_date=eq.{_today_et()}&player=eq.__DIGEST_SENT__"
              f"&stat_type=eq.__marker__")
    req = _r.Request(f"{_SB_URL}/rest/v1/pick_log?{params}",
                     headers=_sb_headers(), method="DELETE")
    try:
        _r.urlopen(req, timeout=10)
    except Exception as ex:
        print(f"[digest] release failed ({ex})")


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
    wnba_hot = _find_wnba_hot(n=3)
    era_under = _find_era_under(n=3)

    def pk(p):
        return float(p.get("park_factor", 0) or 0)

    def vs_pit(p):
        op = (p.get("opp_pitcher") or "").strip()
        return f" vs {op}" if op and op.lower() != "unknown" else ""

    def l5(p):
        """Recent-form tag: how many of the last 5 games cleared this line, so
        the tier label is never trusted blind. A soft run (≤2/5) still inside
        normal variance for an ~80% pick, but the user sees it and decides."""
        rec = (p.get("recent_values") or [])[:5]
        if not rec:
            return ""
        line = float(p.get("line", 0) or 0)
        over = (p.get("direction") != "UNDER")
        hits = sum(1 for v in rec
                   if (v >= line if over else v <= line))
        flag = " ⚠️" if hits <= 2 else ""
        return f" [L5: {hits}/{len(rec)}{flag}]"

    lines.append("")
    lines.append(f"TODAY'S PICKS {_today_et()[5:]}")
    for p in primes:
        lines.append(f"🎯 {p['player']} OVER {p['line']} {p['stat_type']}{vs_pit(p)} "
                     f"(prime) — park {pk(p):.2f}, {_predicted_rate(p)}{l5(p)}")
    for p in stars:
        lines.append(f"⭐ {p['player']} OVER {p['line']} {p['stat_type']}{vs_pit(p)} "
                     f"— park {pk(p):.2f}, {_predicted_rate(p)}{l5(p)}")
    if not primes and not stars:
        lines.append("(no high-signal picks cleared the bar today)")

    lines.append("")
    lines.append("🔒 LOCKS (safer, goblin lines)")
    if locks:
        for p in locks:
            lines.append(f"🔒 {p['player']} OVER {p['line']} "
                         f"{p['stat_type'].replace(' (Goblin)','')}{vs_pit(p)}{l5(p)}")
    else:
        lines.append("(no locks today)")

    if wnba_hot:
        lines.append("")
        lines.append("🏀 WNBA HOT (combo OVER on hot home players, ~80% newer edge):")
        for p in wnba_hot:
            lines.append(f"🏀 {p['player']} OVER {p['line']} "
                         f"{p['stat_type'].replace(' (WNBAhot)','')}{vs_pit(p)}")

    if era_under:
        lines.append("")
        lines.append("⭐ ERA-UNDER STARS (pitcher unders, mild parks — ~80%, promoted):")
        for p in era_under:
            aw = " (away ✅)" if (p.get("home_away") or "").lower() == "away" else ""
            opp = (p.get("opp_team") or "").strip()
            opp_s = f" vs {opp.split()[-1]}" if opp and opp.lower() != "unknown" else ""
            lines.append(f"⭐ {p['player']} UNDER {p['line']} "
                         f"{p['stat_type'].replace(' (ERAunder)','')}{opp_s}{aw}")

    if runs_watch:
        lines.append("")
        lines.append("🧪 RUNS WATCH (experimental, unproven n=31 ~71%):")
        for p in runs_watch:
            lines.append(f"🧪 {p['player']} UNDER {p['line']} "
                         f"{p['stat_type'].replace(' (RunsWatch)','')}")

    # 🎫 Recommended ticket — build the best 3-leg parlay from ONLY the vetted
    # picks, so the user has a ready-made optimal ticket and never reaches for
    # an unvetted "taco" leg (the demonstrated loss driver). Selection is by
    # tier CONVICTION (prime → star → lock), not raw hit rate: the tiers all
    # backtest ~79-81%, so sorting purely by probability made locks always win
    # and flagship star plays never appeared. Lead with the highest-conviction
    # signal, fill remaining slots with the safer locks. Per-leg probabilities
    # (from backtests) still drive the win-% math. Spread across games to avoid
    # same-game correlation. 3 legs = best EV/variance balance.
    #   (tag, selection_priority, per_leg_prob, pick)
    # ERA-under promoted to star tier (2026-08-13, user call): ticket-eligible
    # at star conviction (0.80 backtest). Still logged/graded as (ERAunder) so
    # its real live rate stays visible separately.
    cand = ([("🎯", 3, 0.80, p) for p in primes]
            + [("⭐", 2, 0.79, p) for p in stars]
            + [("⭐", 2, 0.80, p) for p in era_under]
            + [("🔒", 1, 0.81, p) for p in locks])
    cand.sort(key=lambda x: (x[1], x[2]), reverse=True)
    # dedup by game_id where available so we don't stack one game
    picked, seen_games = [], set()
    for tag, _prio, pr, p in cand:
        g = p.get("game_id") or p.get("player")
        if g in seen_games:
            continue
        seen_games.add(g)
        picked.append((tag, pr, p))
        if len(picked) >= 3:
            break
    if len(picked) >= 2:
        joint = 1.0
        for _, pr, _ in picked:
            joint *= pr
        lines.append("")
        lines.append(f"🎫 SUGGESTED {len(picked)}-LEG TICKET (~{joint*100:.0f}% to cash "
                     f"— bet ONLY these, no extra legs):")
        for tag, pr, p in picked:
            st = (p["stat_type"].replace(" (Goblin)", "").replace(" (WNBAhot)", "")
                  .replace(" (ERAunder)", ""))
            dr = "OVER" if p.get("direction") == "OVER" else "UNDER"
            lines.append(f"   {tag} {p['player']} {dr} {p['line']} {st}{l5(p)}")

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
        for p in wnba_hot:
            log_pick(p)
        for p in era_under:
            log_pick(p)
    except Exception as e:
        print(f"[digest] calibration logging failed: {e}")

    return body, len(primes) + len(stars), len(locks)


def main():
    force = os.getenv("FORCE_RESEND", "").lower() == "true"
    if _already_sent() and not force:
        print("Digest already sent today (local marker) — retry slot, exiting.")
        return
    body, n_stars, n_locks = build_digest()
    print(body)
    # Atomically claim today's single send slot BEFORE pushing, so two delayed
    # cron slots can never both notify (the DB unique constraint is the arbiter,
    # not the eventually-consistent actions/cache). force bypasses it.
    if not force and not _claim_send_slot():
        print("Digest already sent today (another slot claimed it) — exiting.")
        return
    try:
        from notify import send_push
        title = f"🎯 Daily Brief — {n_stars} picks, {n_locks} locks"
        if send_push(body, title=title):
            print("\n[pushed via ntfy]")
            _mark_sent()
        else:
            print("\n[push FAILED]")
            _release_send_slot()   # let a later slot retry
    except Exception as e:
        print(f"\n[push failed: {e}]")
        _release_send_slot()


if __name__ == "__main__":
    main()
