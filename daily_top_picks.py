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


def get_top_picks(sports: list[str], n: int = 6) -> dict[str, list[dict]]:
    """
    Run the scanner for each sport and return top N picks per sport.
    Returns {sport: [pick_dict, ...]}
    """
    import scanner_power_parlay as s

    results = {}
    for sport in sports:
        _log(f"Scanning {sport}...")
        try:
            lines = s.fetch_standard_lines([sport])
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
                if result.get("confidence", 0) < 0.62:
                    continue
                scored.append(result)
                time.sleep(0.02)

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

    return results


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
        if proj is not None:
            edge_pct = int((proj - line_val) / (line_val + 1e-9) * 100) if p.get("direction") == "OVER" else int((line_val - proj) / (line_val + 1e-9) * 100)
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


def run():
    _log("Starting daily top picks generation...")

    # Determine which sports have games today
    sports_to_scan = ["MLB", "NBA", "WNBA"]

    picks_by_sport = get_top_picks(sports_to_scan, n=6)

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
    run()
