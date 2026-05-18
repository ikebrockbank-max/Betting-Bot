"""
pp_injury_alert.py — PrizePicks-focused injury alert.

When a player with an active PP line tonight is newly OUT / Doubtful /
Questionable, fires an alert so you can remove them from slips.

Separate from injury_alert.py, which is Kalshi-focused.

Export:
  run_once(seen: dict | None = None) -> dict
"""

import json
import sys
from datetime import datetime, UTC
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from data.injuries import get_injury_report
from data.prizepicks import get_nba_projections
from notify import send_push, send_email, _SIMPLE_WRAP, _SIMPLE_CARD, _simple_row

SEEN_PATH = Path("logs/.seen_pp_injuries.json")
LOG_PATH  = Path("logs/pp_injury_alerts.log")


def _log(msg: str):
    ts   = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def _load_seen() -> dict:
    if SEEN_PATH.exists():
        try:
            return json.loads(SEEN_PATH.read_text())
        except Exception:
            pass
    return {}


def _save_seen(seen: dict):
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps(seen))


def _status_severity(inj: dict) -> int:
    """Return sort priority: 0=OUT/Disqualified, 1=Doubtful/Warning, 2=other."""
    if inj.get("disqualified"):
        return 0
    if inj.get("warning"):
        return 1
    return 2


def _format_email(alerts: list[dict]) -> tuple[str, str, str]:
    """Build (subject, html, plain) for PP injury alert."""
    count   = len(alerts)
    top     = alerts[0]

    subject = (
        f"PP Injury: {top['player']} {top['status']} — "
        f"has {top['stat_type']} {top['line']} tonight"
    )
    if count > 1:
        subject += f" (+{count - 1} more)"

    cards_html = ""
    plain_lines = [subject, ""]

    for a in alerts:
        accent  = "#dc2626" if a["disqualified"] else "#d97706"
        action  = f"{a['player']} — {a['status']}"
        if a["disqualified"]:
            rec = "Remove from all slips immediately!"
        else:
            rec = "Monitor — status may change before tip-off"

        rows = (
            _simple_row("Status",    f"<b style='color:{accent}'>{a['status']}</b>", accent)
            + _simple_row("Injury",  a.get("detail", "Unknown").title(), "#374151")
            + _simple_row("Report",  a.get("headline", "")[:80], "#374151")
            + _simple_row("PP Line", f"{a['stat_type']} {a['line']}", "#1a202c")
            + _simple_row("Action",  rec, accent)
        )

        cards_html += _SIMPLE_CARD.format(
            accent=accent,
            action=action,
            subtitle=f"PP line: {a['stat_type']} {a['line']} tonight",
            rows=rows,
        )

        plain_lines += [
            f"{a['player']} {a['status']} ({a.get('detail', '')})",
            f"  PP line: {a['stat_type']} {a['line']}",
            f"  {a.get('headline', '')}",
            f"  → {rec}",
            "",
        ]

    ts = datetime.now(UTC).strftime("%b %d %Y %H:%M UTC")
    html = _SIMPLE_WRAP.format(
        header_color="#dc2626",
        header_title=f"PP Injury Alert — {count} player{'s' if count > 1 else ''} on report",
        header_sub=f"Active PP lines detected • {ts}",
        body=cards_html,
    )
    plain = "\n".join(plain_lines)
    return subject, html, plain


def run_once(seen: dict | None = None) -> dict:
    """
    Check injury report against tonight's PP lines.
    Returns updated seen dict (persisted to SEEN_PATH).
    """
    if seen is None:
        seen = _load_seen()

    # 1. Tonight's PP lines
    try:
        projs = get_nba_projections(tonight_only=True)
        _log(f"[pp_injury] {len(projs)} PP projections tonight")
    except Exception as e:
        _log(f"[pp_injury] PP fetch failed: {e}")
        return seen

    # Build player -> {stat_type, line} map (pick first line per player)
    pp_players: dict[str, dict] = {}
    for proj in projs:
        name_lower = proj["player"].lower()
        if name_lower not in pp_players:
            pp_players[name_lower] = {
                "player":    proj["player"],
                "stat_type": proj.get("stat_type", ""),
                "line":      proj.get("line", 0),
            }

    if not pp_players:
        _log("[pp_injury] No PP players tonight — skipping")
        return seen

    # 2. Injury report
    try:
        report = get_injury_report()
        _log(f"[pp_injury] {len(report)} players on injury report")
    except Exception as e:
        _log(f"[pp_injury] Injury fetch failed: {e}")
        return seen

    # 3. Cross-reference
    new_alerts: list[dict] = []

    for player_lower, inj in report.items():
        if not (inj.get("disqualified") or inj.get("warning")):
            continue  # healthy

        # Match to a PP player (exact then last-name)
        pp_info = pp_players.get(player_lower)
        if not pp_info:
            last = player_lower.split()[-1]
            for k, v in pp_players.items():
                if k.endswith(last):
                    pp_info = v
                    break

        if not pp_info:
            continue  # not on PP tonight

        seen_key = f"{player_lower}|{inj['status']}"
        if seen_key in seen:
            continue  # already alerted

        alert = {
            "player":       pp_info["player"],
            "player_lower": player_lower,
            "stat_type":    pp_info["stat_type"],
            "line":         pp_info["line"],
            "status":       inj["status"],
            "detail":       inj.get("detail", ""),
            "headline":     inj.get("headline", ""),
            "disqualified": inj.get("disqualified", False),
            "warning":      inj.get("warning", False),
        }
        new_alerts.append(alert)
        seen[seen_key] = inj["status"]

        severity = "OUT" if inj.get("disqualified") else "QUESTIONABLE"
        _log(
            f"[pp_injury] {severity}: {pp_info['player']} — "
            f"PP line: {pp_info['stat_type']} {pp_info['line']}"
        )

    if not new_alerts:
        _log("[pp_injury] No new PP injury alerts.")
        _save_seen(seen)
        return seen

    # Sort: disqualified first
    new_alerts.sort(key=_status_severity)

    # Push
    try:
        top = new_alerts[0]
        push_msg = (
            f"{top['player']} {top['status']} — "
            f"has PP line: {top['stat_type']} {top['line']} tonight. "
            f"Remove from slips!"
        )
        if len(new_alerts) > 1:
            push_msg += f" +{len(new_alerts) - 1} more"
        send_push(push_msg, title="PP Injury Alert!")
        _log(f"[pp_injury] Push sent: {push_msg[:100]}")
    except Exception as e:
        _log(f"[pp_injury] Push error (non-fatal): {e}")

    # Email
    try:
        subject, html, plain = _format_email(new_alerts)
        send_email(subject, html, plain)
        _log(f"[pp_injury] Email sent: {subject}")
    except Exception as e:
        _log(f"[pp_injury] Email error (non-fatal): {e}")

    _save_seen(seen)
    return seen


if __name__ == "__main__":
    result = run_once()
    print(f"Seen dict has {len(result)} entries")
