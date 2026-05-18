"""
scanner_line_movement.py — PP line movement tracker.

Compares current PP lines to the previous scan snapshot and alerts when
any line moves >= 0.5 on any stat. Snapshot stored at logs/.pp_lines_snapshot.json.

Called from auto_scan.run() each cycle.

Export:
  check_line_movements(current_projs: list[dict]) -> list[dict]
"""

import json
import sys
from datetime import datetime, UTC
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from notify import send_push, send_email, _SIMPLE_WRAP, _SIMPLE_CARD, _simple_row

SNAPSHOT_PATH = Path("logs/.pp_lines_snapshot.json")
MIN_MOVE      = 0.5   # minimum line shift to trigger alert


def _load_snapshot() -> dict:
    if SNAPSHOT_PATH.exists():
        try:
            return json.loads(SNAPSHOT_PATH.read_text())
        except Exception:
            pass
    return {}


def _save_snapshot(snapshot: dict):
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(json.dumps(snapshot))


def _build_email(movers: list[dict]) -> tuple[str, str, str]:
    """Build (subject, html, plain) for line movement alert."""
    count   = len(movers)
    subject = f"PP Line Move: {count} line{'s' if count > 1 else ''} shifted"

    # Pick a lead for the subject
    top = movers[0]
    arrow = "↑" if top["direction"] == "UP" else "↓"
    subject = (
        f"PP Line Move: {top['player']} {top['stat']} "
        f"{top['old_line']} → {top['new_line']} {arrow}"
    )
    if count > 1:
        subject += f" (+{count - 1} more)"

    cards_html = ""
    plain_lines = [subject, ""]

    for m in movers:
        arrow   = "↑" if m["direction"] == "UP" else "↓"
        accent  = "#2563eb" if m["direction"] == "UP" else "#7c3aed"
        meaning = (
            "Line moved DOWN — easier to go OVER"
            if m["direction"] == "DOWN"
            else "Line moved UP — easier to go UNDER"
        )

        rows = (
            _simple_row("Player",    m["player"])
            + _simple_row("Stat",    m["stat"])
            + _simple_row("Old line", str(m["old_line"]), "#6b7280")
            + _simple_row("New line", str(m["new_line"]), "#111827")
            + _simple_row("Move",    f"{arrow} {m['move']:+.1f} ({m['direction']})", accent)
            + _simple_row("Meaning", meaning, accent)
        )

        cards_html += _SIMPLE_CARD.format(
            accent=accent,
            action=f"{arrow} {m['player']} {m['stat']}",
            subtitle=f"{m['old_line']} → {m['new_line']}  |  {meaning}",
            rows=rows,
        )

        plain_lines += [
            f"{arrow} {m['player']} {m['stat']}: {m['old_line']} → {m['new_line']} ({m['direction']}, {m['move']:+.1f})",
            f"  {meaning}",
            "",
        ]

    ts = datetime.now(UTC).strftime("%b %d %Y %H:%M UTC")
    html = _SIMPLE_WRAP.format(
        header_color="#1e3a5f",
        header_title=f"PP Line Movement — {count} shift{'s' if count > 1 else ''} detected",
        header_sub=f"Lines compared to last scan snapshot • {ts}",
        body=cards_html,
    )
    plain = "\n".join(plain_lines)
    return subject, html, plain


def check_line_movements(current_projs: list[dict]) -> list[dict]:
    """
    Compare current PP projections to the stored snapshot.
    Saves a new snapshot after comparison.

    Returns list of mover dicts:
      {player, stat, old_line, new_line, move, direction}
    Fires email + push if any movers found.
    """
    snapshot = _load_snapshot()

    # Build current map: "Player|Stat" -> line
    current_map: dict[str, float] = {}
    for proj in current_projs:
        key = f"{proj['player']}|{proj.get('stat_type', proj.get('stat', ''))}"
        current_map[key] = float(proj["line"])

    movers: list[dict] = []

    if snapshot:
        for key, new_line in current_map.items():
            old_line = snapshot.get(key)
            if old_line is None:
                continue
            move = new_line - old_line
            if abs(move) >= MIN_MOVE:
                player, stat = key.split("|", 1)
                movers.append({
                    "player":    player,
                    "stat":      stat,
                    "old_line":  old_line,
                    "new_line":  new_line,
                    "move":      round(move, 2),
                    "direction": "UP" if move > 0 else "DOWN",
                })

    # Save updated snapshot
    _save_snapshot(current_map)

    if movers:
        movers.sort(key=lambda m: abs(m["move"]), reverse=True)

        try:
            top = movers[0]
            arrow = "↑" if top["direction"] == "UP" else "↓"
            push_msg = (
                f"Line moved: {top['player']} {top['stat']} "
                f"{top['old_line']} → {top['new_line']} {arrow}"
            )
            if len(movers) > 1:
                push_msg += f" (+{len(movers) - 1} more)"
            send_push(push_msg, title="PP Line Movement!")
        except Exception as e:
            print(f"[line_movement] Push error (non-fatal): {e}")

        try:
            subject, html, plain = _build_email(movers)
            send_email(subject, html, plain)
        except Exception as e:
            print(f"[line_movement] Email error (non-fatal): {e}")

    return movers


if __name__ == "__main__":
    # Quick test: print current snapshot
    from data.prizepicks import get_nba_projections
    projs = get_nba_projections(tonight_only=True)
    found = check_line_movements(projs)
    print(f"Movers: {len(found)}")
    for m in found:
        print(f"  {m}")
