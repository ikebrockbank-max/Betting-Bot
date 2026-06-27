"""
daily_top_picks.py — Daily top 6 picks per sport notification.

Replaces the old daily digest push notification.
Runs the full scanner across MLB, NBA, and WNBA, takes the top 6
per sport with verified data, and sends:
  1. ntfy push notification (replaces old digest push)
  2. Discord embed (premium channel)

Schedule: 1:00 PM ET daily via GitHub Actions (17:00 UTC)
Run manually: python3 daily_top_picks.py
"""

import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

SPORT_EMOJI = {"MLB": "⚾", "NBA": "🏀", "WNBA": "🏀", "TENNIS": "🎾"}

def _log(msg):
    print(f"[daily_picks {datetime.now(timezone.utc).strftime('%H:%M')}] {msg}", flush=True)


# MLB rank weights, layered on top of the canonical direction/stat-type gate
# below (parlay_builder._passes_direction_gate). That gate already removes the
# confirmed-bad stat types and the UNDER bias; this just orders what's left
# toward the categories with the best track record (14-day review, 2026-06-18).
def _mlb_trust_score(p: dict) -> float:
    stat = p.get("stat_type")
    conf = p.get("confidence", 0)
    # Pitcher Fantasy Score UNDER unlock REVERTED (2026-06-24): the "92.3%"
    # finding was computed against a broken scoring formula across the whole
    # codebase (wrong weights, missing the quality-start bonus, wrongly
    # penalizing hits/walks/HBP that PrizePicks doesn't penalize at all).
    # Caught live when the user checked Kyle Freeland's actual PrizePicks
    # app score (44) against what this system computed (26.1). Formula fixed
    # in calibration_tracker.py and data/mlb_batter_stats.py; recomputing all
    # 192 historical picks with the correct formula shows UNDER at 46.3%
    # (56/121) — a coinflip, not an edge. No replacement signal yet.
    if stat == "Runs":
        return 0.58          # flat ~56-58% across every confidence level (n=66)
    if stat == "Walks":
        return 0.62          # 64.3% overall, but small sample (n=14)
    if stat == "Hitter Fantasy Score":
        if 0.75 <= conf < 0.80:
            return 0.622     # best volume/performance combo (n=74, 62.2%)
        if conf >= 0.80:
            return 0.60      # n=10, slightly below the 0.75-0.80 band
        if conf >= 0.70:
            return 0.52      # n=100, barely above coinflip
        return 0.50          # n=36, coinflip
    return conf


def get_top_picks(sports: list[str], n: int = 6) -> tuple[dict[str, list[dict]], list[str]]:
    """
    Run the scanner for each sport and return top N picks per sport.
    Returns ({sport: [pick_dict, ...]}, [sports whose PP fetch errored]).

    The second element exists so callers can tell "PrizePicks blocked the
    request" apart from "no games today" — both used to produce an
    identical empty result with no visible difference, which is exactly
    how two full days of notifications (2026-06-25, 06-26) went missing
    silently: PrizePicks returned 403/429 to the GitHub Actions IP, and it
    looked indistinguishable from a legitimate slate-free day until someone
    checked the raw logs by hand.
    """
    import scanner_power_parlay as s
    # This is the same direction/stat-type gate parlays already use (blocks
    # EXCLUDED_STAT_TYPES like Singles/Hits Allowed/Pitcher Strikeouts, and
    # blocks UNDER outside UNDER_EXCEPTIONS — confirmed UNDER 47% vs OVER 56%,
    # n=706 — see parlay_builder.py). The single-pick ntfy push never applied
    # it before, so it was pushing picks the parlay builder itself would reject.
    from parlay_builder import _passes_direction_gate

    results = {}
    fetch_failures = []
    for sport in sports:
        _log(f"Scanning {sport}...")
        try:
            lines = s.fetch_standard_lines([sport])
            if getattr(s, "_last_fetch_failures", None):
                fetch_failures.extend(s._last_fetch_failures)
            if not lines:
                _log(f"  {sport}: no lines today")
                results[sport] = []
                continue

            scored = []
            for pick in lines:
                stats = s.get_stats_for_pick(pick)
                if not stats:
                    continue
                result = s.score_pick(stats, pick)
                if result.get("skip_reason"):
                    continue
                # Matches MIN_CONF_PARLAY in parlay_builder.py. Their signal_miner
                # data (728 picks, 2026-06-15) confirmed 65-70% confidence is a
                # negative-EV dead zone (47% actual) and 70-75% is barely above
                # baseline (53%). 75%+ is the only bucket with real significance
                # (61.2% actual, z=2.0). No reason the single-pick push should
                # tolerate a range the parlay builder itself rejects.
                #
                # A Pitcher Fantasy Score UNDER exception was added and then
                # REVERTED the same day (2026-06-24) — see _mlb_trust_score
                # for the full story. It was built on a broken scoring formula.
                if result.get("confidence", 0) < 0.75:
                    continue
                if not _passes_direction_gate(result):
                    continue
                scored.append(result)
                time.sleep(0.02)

            if sport == "MLB":
                scored.sort(key=_mlb_trust_score, reverse=True)
            else:
                scored.sort(key=lambda x: x["confidence"], reverse=True)

            # Deduplicate by player — keep best pick per player
            seen_players = set()
            top = []
            for p in scored:
                key = p["player"]
                if key not in seen_players:
                    seen_players.add(key)
                    top.append(p)
                if len(top) >= n:
                    break

            results[sport] = top
            _log(f"  {sport}: {len(scored)} qualified → top {len(top)}")

        except Exception as e:
            _log(f"  {sport} scan failed: {e}")
            results[sport] = []
            fetch_failures.append(sport)

    return results, fetch_failures


def _build_advanced_note(p: dict) -> str:
    """
    One-line advanced context note for a pick.
    MLB: H2H record vs pitcher/team, key Statcast stat.
    NBA/WNBA: vs-opponent historical avg, rest days.
    """
    sport = p.get("sport", "")
    notes = []

    if sport == "MLB":
        try:
            from data.mlb_h2h import format_h2h_note, format_pitcher_h2h_note
            is_pitcher = p.get("stat_type") in (
                "Pitcher Strikeouts", "Strikeouts", "Pitcher Fantasy Score",
                "Pitching Outs", "Earned Runs Allowed", "Hits Allowed",
                "Walks Allowed", "Pitches Thrown"
            )
            sc = p.get("statcast", {})
            if is_pitcher:
                pvt  = p.get("pitcher_vs_team")
                opp  = p.get("opp_team", "")
                note = format_pitcher_h2h_note(pvt, opp)
                if note:
                    notes.append(note)
                # Pitcher Statcast: xBA allowed, barrel% allowed
                sc_bits = []
                if sc.get("xba_allowed") is not None:
                    sc_bits.append(f"xBA-alwd {sc['xba_allowed']:.3f}")
                if sc.get("barrel_pct") is not None:
                    sc_bits.append(f"Brl-alwd {sc['barrel_pct']*100:.1f}%")
                if sc_bits:
                    notes.append(" ".join(sc_bits))
            else:
                note = format_h2h_note(
                    p.get("h2h"), p.get("vs_team"), sc,
                    p.get("opp_pitcher", ""), p.get("opp_team", ""), is_pitcher=False
                )
                if note:
                    notes.append(note)
        except Exception:
            pass

    elif sport == "WNBA":
        # Projection-first format (ChatGPT recommendation)
        proj     = p.get("projected_stat")
        proj_low = p.get("proj_low")
        proj_hi  = p.get("proj_high")
        pm       = p.get("projected_minutes")
        std      = p.get("min_std_dev")
        line_val = p.get("line", 0)
        p_over   = p.get("p_over")
        p_under  = p.get("p_under")
        direction = p.get("direction", "OVER")
        if proj is not None:
            edge_pct = int((proj - line_val) / (line_val + 1e-9) * 100) if direction == "OVER" else int((line_val - proj) / (line_val + 1e-9) * 100)
            # Include P(direction) when the probability engine fired
            if p_over is not None and p_under is not None:
                p_dir = p_over if direction == "OVER" else p_under
                notes.append(
                    f"Proj {proj} (range {proj_low}–{proj_hi}) | "
                    f"P({direction.capitalize()}): {int(p_dir*100)}% | Edge {edge_pct:+d}%"
                )
            else:
                notes.append(f"Proj {proj} (range {proj_low}–{proj_hi}) | Edge {edge_pct:+d}%")
        if pm is not None:
            std_str = f"±{std}" if std else ""
            notes.append(f"Exp {pm}{std_str} min")
        # H2H
        h2h = p.get("wnba_h2h")
        opp = p.get("opp_team", "")
        if h2h and h2h.get("n", 0) >= 2:
            notes.append(f"H2H vs {opp.split()[-1]}: {h2h['avg']} ({h2h['n']}G)")
        # Home/away split
        ha    = p.get("home_away", "")
        split = p.get("home_split") if ha == "home" else p.get("away_split")
        if split and split.get("n", 0) >= 4:
            notes.append(f"{ha.capitalize()} avg {split['avg']} ({split['n']}G)")
        # Rest
        rest = p.get("rest_days")
        if rest == 0:
            notes.append("⚠️ B2B")
        elif rest == 1:
            notes.append("⚠️ 1d rest")
        # Role stability
        cv = p.get("role_stability")
        if cv is not None and cv < 0.4:
            notes.append("⚠️ volatile mins")
        # Usage trend — show when meaningfully elevated or depressed
        # Also flag if the spike was noisy (usage_confidence < 0.60)
        usage_trend      = p.get("usage_trend")
        usage_adj        = p.get("usage_adj", 1.0)
        usage_confidence = p.get("usage_confidence", 1.0)
        if usage_trend is not None and abs(usage_trend) >= 0.10:
            pct  = int(usage_trend * 100)
            icon = "📈" if usage_trend > 0 else "📉"
            note = f"{icon} Usage {pct:+d}% (shots/min vs season)"
            if usage_confidence < 0.60:
                note += f" [noisy, {int(usage_confidence*100)}% conf]"
            notes.append(note)
        # Injury impact — show method so it's clear if boost is evidence-based
        inj_note = p.get("injury_note", "")
        inj_src  = p.get("injury_adjustment_source", "")
        if inj_note:
            # Replace generic emoji with WOWY indicator if available
            if inj_src == "WOWY" and "📈" not in inj_note:
                inj_note = inj_note.replace("🏥", "📈 WOWY")
            notes.append(inj_note)

    elif sport == "NBA":
        vs_avg = p.get("nba_vs_opp_avg")
        opp    = p.get("opp_team", "")
        if vs_avg is not None and opp and opp != "unknown":
            opp_short = opp.split()[-1]
            notes.append(f"vs {opp_short} avg: {vs_avg}")
        rest = p.get("rest_days")
        pg   = p.get("playoff_games", 0) or 0
        if pg:
            notes.append(f"Playoffs ({pg}G)")
        if rest == 0:
            notes.append("⚠️ B2B")
        elif rest is not None and rest >= 3 and not pg:
            notes.append(f"✅ {rest}d rest")
        mflag  = p.get("minutes_flag")
        pm_avg = p.get("playoff_min_avg")
        if mflag == "elevated":
            notes.append(f"✅ Min↑{f' ({pm_avg}mpg)' if pm_avg else ''}")
        elif mflag == "reduced":
            notes.append(f"⚠️ Min↓{f' ({pm_avg}mpg)' if pm_avg else ''}")
        elif pm_avg:
            notes.append(f"{pm_avg}mpg playoffs")
        rim = p.get("rim_note", "")
        if rim:
            notes.append(rim)
        inj_note = p.get("injury_note", "")
        if inj_note:
            notes.append(inj_note)

    # MLB weather note
    if sport == "MLB":
        w = p.get("weather_note")
        if w:
            notes.append(w)

    # Key matchup context notes (only signal-bearing ones)
    ctx_notes = p.get("context_notes", [])
    filtered = [n for n in ctx_notes if any(sig in n for sig in ("✅", "⚠️", "High-K", "Low-K"))]
    if filtered and not any(f in " ".join(notes) for f in ["K lineup", "K line"]):
        notes.append(filtered[0])

    return " | ".join(notes) if notes else ""


def format_ntfy_push(picks_by_sport: dict[str, list]) -> tuple[str, str]:
    """
    Format the ntfy push notification.
    Returns (title, body) — ntfy has ~4KB body limit.
    """
    now_et = datetime.now(timezone.utc) - timedelta(hours=4)
    date_str = now_et.strftime("%b %-d")

    title = f"⚡ Sharp Lines — {date_str}"

    body_lines = []
    total_picks = 0

    for sport, picks in picks_by_sport.items():
        if not picks:
            continue
        emoji = SPORT_EMOJI.get(sport, "🎯")
        body_lines.append(f"{emoji} {sport}")

        for i, p in enumerate(picks, 1):
            conf  = p["conf_pct"]
            hits  = p["over_hits"] if p["direction"] == "OVER" else p["under_hits"]
            n     = p["n_games"]
            arrow = "↑" if p["direction"] == "OVER" else "↓"
            opp   = p.get("opp_team", "")
            opp_s = f" vs {opp.split()[-1]}" if opp and opp != "unknown" else ""

            body_lines.append(
                f"{i}. {p['player']} {arrow}{p['line']} {p['stat_type']} "
                f"({conf}% | {hits}/{n}){opp_s}"
            )

            # Advanced note (H2H, Statcast, vs-opp avg)
            adv = _build_advanced_note(p)
            if adv:
                body_lines.append(f"   {adv}")

            total_picks += 1

        body_lines.append("")

    if total_picks == 0:
        return title, "No qualified picks today."

    # Add best parlay at bottom
    all_picks = [p for picks in picks_by_sport.values() for p in picks]
    if len(all_picks) >= 2:
        import scanner_power_parlay as s
        parlays = s.build_parlays(all_picks[:20])
        if parlays:
            best = parlays[0]
            legs = " + ".join(
                f"{l['player'].split()[-1]} {l['direction']} {l['line']}"
                for l in best["legs"]
            )
            body_lines.append(
                f"🎯 Best {best['n_legs']}-leg ({best['payout']}x): {legs}"
            )
            body_lines.append(
                f"   Win prob: {int(best['p_win']*100)}% | EV: +{best['ev_pct']}%"
            )

    return title, "\n".join(body_lines)


def format_discord_embed(picks_by_sport: dict[str, list]) -> dict:
    """Rich Discord embed with full stats per pick."""
    now_et  = datetime.now(timezone.utc) - timedelta(hours=4)
    date_str = now_et.strftime("%B %-d, %Y at %-I:%M %p ET")

    fields = []

    for sport, picks in picks_by_sport.items():
        if not picks:
            continue
        emoji = SPORT_EMOJI.get(sport, "🎯")

        pick_lines = []
        for p in picks:
            conf  = p["conf_pct"]
            hits  = p["over_hits"] if p["direction"] == "OVER" else p["under_hits"]
            n     = p["n_games"]
            arrow = "📈" if p["direction"] == "OVER" else "📉"
            opp   = p.get("opp_team", "")
            ha    = p.get("home_away", "")
            opp_p = p.get("opp_pitcher", "")
            loc_hr = p.get("location_hit_rate", p.get("hit_rate", 0))
            rec   = p.get("recent_values", [])[:5]
            conf_bar = "🟢" if conf >= 75 else "🟡"

            loc_note = ""
            if opp and opp != "unknown":
                loc_note = f" · {ha} vs {opp.split()[-1]}"
                if opp_p:
                    loc_note += f" ({opp_p.split()[-1]})"

            # Advanced context line
            adv_parts = []
            sport = p.get("sport", "")
            if sport == "MLB":
                h2h      = p.get("h2h")
                vs_team  = p.get("vs_team")
                pvt      = p.get("pitcher_vs_team")
                sc       = p.get("statcast", {})
                is_pitcher = p.get("stat_type") in (
                    "Pitcher Strikeouts", "Strikeouts", "Pitcher Fantasy Score",
                    "Pitching Outs", "Earned Runs Allowed",
                )
                if is_pitcher and pvt:
                    k_pct = pvt.get("k_pct")
                    era   = pvt.get("era")
                    adv_parts.append(
                        f"vs {opp.split()[-1]}: {pvt['k']}K/{pvt['bf']}BF"
                        + (f" ({k_pct*100:.0f}%K)" if k_pct else "")
                        + (f" ERA {era:.2f}" if era else "")
                    )
                elif h2h:
                    adv_parts.append(
                        f"H2H vs {opp_p.split()[-1] if opp_p else 'P'}: "
                        f"{h2h['h']}/{h2h['ab']} ({h2h['avg']}) · "
                        f"{h2h.get('k',0)}K {h2h.get('bb',0)}BB · OPS {h2h.get('ops','.---')}"
                    )
                elif vs_team:
                    adv_parts.append(
                        f"vs {opp.split()[-1]} career: {vs_team['h']}/{vs_team['ab']} ({vs_team['avg']})"
                    )
                if sc:
                    sc_bits = []
                    if sc.get("barrel_pct") is not None:
                        sc_bits.append(f"Brl {sc['barrel_pct']*100:.1f}%")
                    if sc.get("exit_velo") is not None:
                        sc_bits.append(f"EV {sc['exit_velo']:.1f}")
                    elif sc.get("hard_hit_pct") is not None:
                        sc_bits.append(f"HH {sc['hard_hit_pct']*100:.1f}%")
                    if sc.get("xba") is not None:
                        sc_bits.append(f"xBA {sc['xba']:.3f}")
                    if sc_bits:
                        adv_parts.append("Statcast: " + " · ".join(sc_bits))
            elif sport in ("NBA", "WNBA"):
                vs_avg = p.get("nba_vs_opp_avg")
                if vs_avg is not None:
                    adv_parts.append(f"vs {opp.split()[-1]} avg: {vs_avg}")

            ctx_desc = p.get("context_notes", [])
            if ctx_desc:
                adv_parts.extend(ctx_desc[:2])

            # Projection drivers (ChatGPT recommendation)
            d_pos = p.get("drivers_pos", [])
            d_neg = p.get("drivers_neg", [])
            driver_line = ""
            if d_pos or d_neg:
                all_drivers = d_pos[:3] + d_neg[:2]
                driver_line = "Drivers: " + " | ".join(all_drivers)

            # Statcast quality note
            sc_qual = p.get("statcast_note", "")
            if sc_qual and sc_qual not in " ".join(adv_parts):
                adv_parts.append(f"Statcast: {sc_qual}")

            adv_line = "\n  ".join(filter(None, adv_parts)) if adv_parts else ""

            pick_lines.append(
                f"{conf_bar}{arrow} **{p['player']}** {p['direction']} **{p['line']}** "
                f"{p['stat_type']}\n"
                f"  {conf}% conf · {hits}/{n} hit ({p['hit_rate']:.0%}) · "
                f"avg {p['avg']} · L5: `{rec}`{loc_note}"
                + (f"\n  {adv_line}" if adv_line else "")
                + (f"\n  {driver_line}" if driver_line else "")
            )

        fields.append({
            "name":   f"{emoji} {sport} — Top {len(picks)} Picks",
            "value":  "\n\n".join(pick_lines),
            "inline": False,
        })

    # Parlay field
    all_picks = [p for picks in picks_by_sport.values() for p in picks]
    if len(all_picks) >= 2:
        try:
            import scanner_power_parlay as s
            parlays = s.build_parlays(all_picks[:20])
            if parlays:
                parlay_lines = []
                for par in parlays[:3]:
                    legs = " + ".join(
                        f"{l['player']} {l['direction']} {l['line']} {l['stat_type']}"
                        for l in par["legs"]
                    )
                    hits_per = [
                        (l["over_hits"] if l["direction"]=="OVER" else l["under_hits"],
                         l["n_games"]) for l in par["legs"]
                    ]
                    hit_str = " · ".join(f"{h}/{n}" for h, n in hits_per)
                    parlay_lines.append(
                        f"**{par['n_legs']}-leg ({par['payout']}x):** {legs}\n"
                        f"  Win prob: {int(par['p_win']*100)}% · EV: +{par['ev_pct']}% · "
                        f"Hit rates: {hit_str}"
                    )
                fields.append({
                    "name":  "🎯 Best Parlays",
                    "value": "\n\n".join(parlay_lines),
                    "inline": False,
                })
        except Exception:
            pass

    sports_scanned = [s for s, picks in picks_by_sport.items() if picks]
    total = sum(len(v) for v in picks_by_sport.values())

    return {
        "embeds": [{
            "title":       f"⚡ Daily Top Picks — {date_str}",
            "description": (f"**{total} picks** across {', '.join(sports_scanned)}. "
                            f"Standard lines only · Verified data · All with matchup context."),
            "color":       3066993,
            "fields":      fields[:25],
            "footer":      {
                "text": "SharpLines · sharplines.gg · Calibrated hit-rate model"
            },
        }]
    }


def send_notifications(picks_by_sport: dict[str, list]):
    """Send ntfy push + Discord embed."""
    from notify import send_push

    # ntfy push
    title, body = format_ntfy_push(picks_by_sport)
    push_ok = send_push(body, title=title)
    _log(f"ntfy push: {'sent' if push_ok else 'FAILED'}")
    _log(f"Body preview:\n{body[:300]}")

    # Discord — premium (real-time) and free (60-min delay)
    DISCORD_PREMIUM = os.getenv("DISCORD_WEBHOOK_PREMIUM", "")
    DISCORD_FREE    = os.getenv("DISCORD_WEBHOOK_FREE", "")

    embed = format_discord_embed(picks_by_sport)

    if DISCORD_PREMIUM:
        try:
            resp = requests.post(DISCORD_PREMIUM, json=embed, timeout=10)
            resp.raise_for_status()
            _log("Discord premium: sent")
        except Exception as e:
            _log(f"Discord premium FAILED: {e}")

    if DISCORD_FREE:
        import threading
        def _delayed():
            time.sleep(3600)
            try:
                requests.post(DISCORD_FREE, json=embed, timeout=10)
                _log("Discord free (delayed): sent")
            except Exception:
                pass
        threading.Thread(target=_delayed, daemon=True).start()


def run(sports: list[str] | None = None):
    _log("Starting daily top picks generation...")

    # Allows isolating a single sport for diagnosis (e.g. one scan hanging
    # near the timeout) without scanning the others. Defaults to all three.
    sports_to_scan = sports or ["MLB", "NBA", "WNBA"]
    _log(f"Scoping to: {', '.join(sports_to_scan)}")

    picks_by_sport, fetch_failures = get_top_picks(sports_to_scan, n=6)

    if fetch_failures:
        _log(f"PrizePicks fetch failed for: {', '.join(fetch_failures)}")
        try:
            from notify import send_push
            send_push(
                f"PrizePicks fetch failed for: {', '.join(fetch_failures)}. "
                f"No picks could be scanned for these sports today — this is "
                f"a blocked/rate-limited request, not a real no-games day.",
                title="⚠️ Daily Picks: PrizePicks fetch failed",
            )
        except Exception as e:
            _log(f"Failure alert push also failed: {e}")

    total = sum(len(v) for v in picks_by_sport.values())
    if total == 0:
        _log("No qualified picks today — skipping notification")
        return False

    send_notifications(picks_by_sport)

    # Log every sent pick to calibration tracker
    try:
        from calibration_tracker import log_pick as _log_pick
        for picks in picks_by_sport.values():
            for p in picks:
                _log_pick(p)
        _log(f"Logged {total} picks to calibration tracker")
    except Exception as e:
        _log(f"Calibration logging failed: {e}")

    _log(f"Done. Sent {total} picks across {len([s for s in picks_by_sport if picks_by_sport[s]])} sports.")
    return True


if __name__ == "__main__":
    # python3 daily_top_picks.py MLB         -> single sport
    # python3 daily_top_picks.py MLB,WNBA    -> subset
    # python3 daily_top_picks.py             -> all three (default)
    _arg = sys.argv[1] if len(sys.argv) > 1 else None
    run(sports=[s.strip() for s in _arg.split(",")] if _arg else None)
