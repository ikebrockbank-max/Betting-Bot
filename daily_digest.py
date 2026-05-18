"""
daily_digest.py — Daily Best Plays email digest.

Runs once per day at DAILY_DIGEST_HOUR_UTC (env var, default "15" = 11am ET).
Pulls tonight's top picks via pp_playoff_report logic, appends hit rate stats,
and sends a single clean summary email.

Tracked via logs/.last_daily_digest.json {"date": "YYYY-MM-DD"}.

Export:
  run()
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from notify import send_push, send_email, _SIMPLE_WRAP, _SIMPLE_CARD, _simple_row

LAST_DIGEST_PATH = Path("logs/.last_daily_digest.json")
LOG_PATH         = Path("logs/daily_digest.log")
DAILY_DIGEST_HOUR_UTC = int(os.getenv("DAILY_DIGEST_HOUR_UTC", "15"))


def _log(msg: str):
    ts   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def _load_last_date() -> str:
    if LAST_DIGEST_PATH.exists():
        try:
            data = json.loads(LAST_DIGEST_PATH.read_text())
            return data.get("date", "")
        except Exception:
            pass
    return ""


def _save_last_date(date_str: str):
    LAST_DIGEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAST_DIGEST_PATH.write_text(json.dumps({"date": date_str}))


def _pick_row_html(p: dict, rank: int) -> str:
    d_color = "#16a34a" if p["direction"] == "OVER" else "#dc2626"
    badge   = f"{p['direction']} {p['line']}"
    reason  = p.get("reason", "")[:100]
    return (
        f'<tr style="border-bottom:1px solid #f3f4f6;">'
        f'<td style="padding:8px 4px;font-size:13px;font-weight:700;color:#111827;">#{rank} {p["player"]}</td>'
        f'<td style="padding:8px 4px;font-size:12px;color:#6b7280;">{p["stat_type"]}</td>'
        f'<td style="padding:8px 4px;" align="center">'
        f'<span style="background:{d_color};color:#fff;font-size:11px;font-weight:700;'
        f'padding:2px 9px;border-radius:12px;">{badge}</span></td>'
        f'<td style="padding:8px 4px;font-size:13px;font-weight:800;color:{d_color};" align="right">'
        f'{int(p["prob"] * 100)}%</td>'
        f'</tr>'
        f'<tr><td colspan="4" style="padding:0 4px 8px;font-size:11px;color:#6b7280;">{reason}</td></tr>'
    )


def _parlay_summary_html(parlay: dict) -> str:
    if not parlay:
        return ""
    legs = " + ".join(
        f"{pk['player'].split()[-1]} {pk['direction']} {pk['line']}"
        for pk in parlay["picks"]
    )
    combined = int(parlay["combined"] * 100)
    payout   = parlay["payout"]
    return (
        f'<p style="margin:0 0 6px;font-size:14px;font-weight:700;color:#1e1b4b;">'
        f'{parlay["size"]}-Pick Parlay ({payout}x payout, {combined}% combined)</p>'
        f'<p style="margin:0 0 12px;font-size:13px;color:#374151;">{legs}</p>'
    )


def _build_email(
    top_picks: list[dict],
    best_2: dict | None,
    best_3: dict | None,
    summary: dict,
    date_str: str,
    games: list[dict],
) -> tuple[str, str, str]:
    subject = f"PP Best Plays - {date_str}"

    # ── Section 1: Top picks table ───────────────────────────────────────────
    pick_rows = "".join(_pick_row_html(p, i + 1) for i, p in enumerate(top_picks[:5]))
    picks_section = f"""\
<p style="margin:0 0 10px;font-size:15px;font-weight:800;color:#1e1b4b;">
  Top Individual Picks</p>
<table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:20px;">
  {pick_rows}
</table>"""

    # ── Section 2: Parlay recommendations ────────────────────────────────────
    parlay_section = ""
    if best_2 or best_3:
        parlay_section += (
            '<p style="margin:0 0 10px;font-size:15px;font-weight:800;color:#1e1b4b;">'
            'Best Parlays</p>'
        )
        if best_2:
            parlay_section += _parlay_summary_html(best_2)
        if best_3:
            parlay_section += _parlay_summary_html(best_3)

    # ── Section 3: Bot performance ───────────────────────────────────────────
    total    = summary.get("total", 0)
    hit_rate = summary.get("hit_rate", 0.0)
    perf_color = "#16a34a" if hit_rate >= 0.58 else "#d97706" if hit_rate >= 0.50 else "#dc2626"

    perf_section = (
        f'<table width="100%" cellpadding="0" cellspacing="0"'
        f' style="margin-top:8px;background:#f8fafc;border-radius:8px;overflow:hidden;'
        f'border:1px solid #e5e7eb;">'
        f'<tr><td style="background:#1e1b4b;padding:10px 14px;">'
        f'<p style="margin:0;color:#fff;font-size:14px;font-weight:700;">Bot Performance (Last 30 Days)</p>'
        f'</td></tr>'
        f'<tr><td style="padding:12px 14px;">'
        f'{_simple_row("Picks logged", str(total))}'
        f'{_simple_row("Hit rate", f"{hit_rate:.1%}", perf_color)}'
    )
    for stat, d in list(summary.get("by_stat", {}).items())[:4]:
        perf_section += _simple_row(stat, f"{d['hits']}/{d['total']} ({d['rate']:.1%})")
    perf_section += '</td></tr></table>'

    # ── Footer: game times ────────────────────────────────────────────────────
    footer_lines = []
    for g in games:
        from datetime import timedelta
        et = (g["start_time"] - timedelta(hours=4)).strftime("%I:%M %p ET").lstrip("0")
        footer_lines.append(f"{g.get('away_full', '')} @ {g.get('home_full', '')} — {et}")
    footer_games = (
        '<p style="margin:16px 0 4px;font-size:12px;font-weight:700;color:#374151;">Tonight\'s Games</p>'
        + "".join(
            f'<p style="margin:0 0 3px;font-size:12px;color:#6b7280;">{ln}</p>'
            for ln in footer_lines
        )
        if footer_lines else ""
    )
    footer_note = (
        '<p style="margin:12px 0 0;font-size:11px;color:#9ca3af;">'
        'Always verify lines in the PP app before placing. Lines can move up to tip-off.</p>'
    )

    body = picks_section + parlay_section + perf_section + footer_games + footer_note

    ts = datetime.now(timezone.utc).strftime("%b %d %Y %H:%M UTC")
    html = _SIMPLE_WRAP.format(
        header_color="#1e1b4b",
        header_title=f"Today's Best Plays — {date_str}",
        header_sub=f"Generated {ts}",
        body=body,
    )

    # Plain text
    plain_lines = [f"PP Best Plays — {date_str}", ""]
    plain_lines.append("TOP PICKS:")
    for i, p in enumerate(top_picks[:5], 1):
        plain_lines.append(
            f"  #{i} {p['player']} {p['direction']} {p['line']} {p['stat_type']} — {int(p['prob']*100)}%"
        )
        plain_lines.append(f"     {p.get('reason','')[:100]}")
    plain_lines.append("")
    if best_2:
        legs = " + ".join(f"{pk['player'].split()[-1]} {pk['direction']}" for pk in best_2["picks"])
        plain_lines.append(f"2-PICK PARLAY: {legs} | {int(best_2['combined']*100)}% | {best_2['payout']}x")
    if best_3:
        legs = " + ".join(f"{pk['player'].split()[-1]} {pk['direction']}" for pk in best_3["picks"])
        plain_lines.append(f"3-PICK PARLAY: {legs} | {int(best_3['combined']*100)}% | {best_3['payout']}x")
    plain_lines.append("")
    plain_lines.append(f"BOT PERFORMANCE (last 30 days): {total} picks, {hit_rate:.1%} hit rate")

    return subject, html, "\n".join(plain_lines)


def run():
    """
    Run the daily digest. Called by scheduler.py once per day when
    current UTC hour >= DAILY_DIGEST_HOUR_UTC.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Guard: only fire once per day
    if _load_last_date() == today:
        _log(f"[digest] Already sent for {today} — skipping")
        return

    _log(f"[digest] Running daily digest for {today}")

    # Import pp_playoff_report functions
    try:
        from pp_playoff_report import (
            fetch_pp_projections,
            score_all_picks,
            build_parlays,
            get_todays_games,
        )
        from data.injuries import get_injury_report
    except Exception as e:
        _log(f"[digest] Import error: {e}")
        return

    # Check for games tonight
    try:
        games = get_todays_games()
        if not games:
            _log("[digest] No NBA games today — skipping digest")
            return
        _log(f"[digest] {len(games)} game(s) tonight")
    except Exception as e:
        _log(f"[digest] Game fetch error: {e}")
        return

    # Fetch projections
    try:
        projs = fetch_pp_projections()
        if not projs:
            _log("[digest] No PP projections — skipping digest")
            return
        _log(f"[digest] {len(projs)} projections fetched")
    except Exception as e:
        _log(f"[digest] PP fetch error: {e}")
        return

    # Injuries
    try:
        injury_report = get_injury_report()
        _log(f"[digest] {len(injury_report)} players on injury report")
    except Exception as e:
        _log(f"[digest] Injury fetch error (non-fatal): {e}")
        injury_report = {}

    # Score picks
    try:
        all_picks = score_all_picks(projs, injury_report)
        _log(f"[digest] {len(all_picks)} qualifying picks scored")
    except Exception as e:
        _log(f"[digest] Score error: {e}")
        return

    if not all_picks:
        _log("[digest] No qualifying picks tonight — skipping digest")
        return

    # Build parlays
    try:
        parlays = build_parlays(all_picks)
        _log(f"[digest] {len(parlays)} parlays built")
    except Exception as e:
        _log(f"[digest] Parlay error (non-fatal): {e}")
        parlays = []

    best_2 = next((p for p in parlays if p["size"] == 2), None)
    best_3 = next((p for p in parlays if p["size"] == 3), None)

    # Get hit tracker summary
    try:
        from hit_tracker import get_summary
        summary = get_summary()
    except Exception as e:
        _log(f"[digest] hit_tracker error (non-fatal): {e}")
        summary = {"total": 0, "hits": 0, "hit_rate": 0.0, "by_stat": {}}

    # Format and send
    try:
        date_display = datetime.now(timezone.utc).strftime("%B %d, %Y")
        subject, html, plain = _build_email(all_picks, best_2, best_3, summary, date_display, games)

        push_body = f"{len(all_picks)} picks tonight"
        if best_2:
            legs = " + ".join(p["player"].split()[-1] for p in best_2["picks"])
            push_body = f"Best 2-pick: {legs} ({int(best_2['combined']*100)}%)"

        send_push(push_body, title=f"PP Digest: {date_display}")
        send_email(subject, html, plain)
        _log(f"[digest] Sent: {subject}")
    except Exception as e:
        _log(f"[digest] Send error: {e}")
        return

    _save_last_date(today)


if __name__ == "__main__":
    run()
