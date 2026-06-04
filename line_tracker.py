"""
line_tracker.py — Snapshot PrizePicks lines on every scan to detect movement.

Every time fetch_standard_lines() runs, we store the current line for every player.
Over time this builds a history that reveals:
  - Which lines are steaming (books moving against you)
  - Which lines are soft (no movement = books not worried)
  - Whether our best picks get adjusted before game time

Storage: logs/line_history.json
Format:
  {
    "{player}|{stat_type}": [
      {"line": 18.5, "ts": "2026-06-04T14:00:00Z"},
      {"line": 19.5, "ts": "2026-06-04T17:00:00Z"},
      ...
    ]
  }
"""

import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

HISTORY_PATH = Path("logs/line_history.json")
MAX_SNAPSHOTS_PER_KEY = 24   # ~1 day of hourly snapshots per prop
HISTORY_TTL_DAYS      = 7    # prune entries older than 7 days


# ── Storage ───────────────────────────────────────────────────────────────────

def _load() -> dict:
    if HISTORY_PATH.exists():
        try:
            return json.loads(HISTORY_PATH.read_text())
        except Exception:
            pass
    return {}

def _save(data: dict):
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps(data))


# ── Snapshot ──────────────────────────────────────────────────────────────────

def snapshot_lines(lines: list[dict]):
    """
    Store current PP lines. Called after every fetch_standard_lines().
    Prunes entries older than HISTORY_TTL_DAYS automatically.
    """
    now_iso  = datetime.now(timezone.utc).isoformat()
    now_ts   = time.time()
    cutoff   = now_ts - (HISTORY_TTL_DAYS * 86400)

    history = _load()

    for pick in lines:
        key = f"{pick['player']}|{pick['stat_type']}"
        if key not in history:
            history[key] = []

        snapshots = history[key]

        # Only add if line changed or it's been > 30 mins since last snapshot
        if snapshots:
            last = snapshots[-1]
            same_line   = abs(last.get("line", 0) - pick["line"]) < 0.01
            recent_snap = (now_ts - last.get("ts_unix", 0)) < 1800
            if same_line and recent_snap:
                continue

        snapshots.append({
            "line":    pick["line"],
            "ts":      now_iso,
            "ts_unix": now_ts,
        })

        # Prune old snapshots
        snapshots = [s for s in snapshots if s.get("ts_unix", 0) > cutoff]
        # Cap per key
        history[key] = snapshots[-MAX_SNAPSHOTS_PER_KEY:]

    _save(history)


# ── Line movement ─────────────────────────────────────────────────────────────

def get_line_movement(player: str, stat_type: str) -> dict | None:
    """
    Returns movement info for a player/stat pair.
    {
        opening_line:  float,   # first snapshot today
        current_line:  float,   # most recent snapshot
        movement:      float,   # current - opening (+ = line went up)
        n_snapshots:   int,
        hours_tracked: float,
        steaming:      bool,    # moved more than 0.5 in same direction
    }
    """
    history = _load()
    key = f"{player}|{stat_type}"
    snaps = history.get(key, [])

    # Only look at today's snapshots
    today_str = (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%Y-%m-%d")
    today_snaps = [s for s in snaps if s.get("ts", "")[:10] == today_str]

    if len(today_snaps) < 2:
        return None

    opening = today_snaps[0]["line"]
    current = today_snaps[-1]["line"]
    movement = round(current - opening, 2)

    ts_first = today_snaps[0].get("ts_unix", 0)
    ts_last  = today_snaps[-1].get("ts_unix", 0)
    hours_tracked = round((ts_last - ts_first) / 3600, 1)

    return {
        "opening_line":  opening,
        "current_line":  current,
        "movement":      movement,
        "n_snapshots":   len(today_snaps),
        "hours_tracked": hours_tracked,
        "steaming":      abs(movement) >= 0.5,
        "history":       [(s["line"], s["ts"][11:16]) for s in today_snaps],
    }


def line_movement_signal(player: str, stat_type: str, direction: str) -> tuple[float, str]:
    """
    Returns (adjustment, note) for line movement.
    adjustment: [-0.04, 0.04] confidence adjustment
      - Line moved UP + OVER pick   = market agrees = +signal
      - Line moved UP + UNDER pick  = market disagrees = -signal
      - No movement or tiny = 0.0

    Returns (0.0, "") if insufficient data.
    """
    mv = get_line_movement(player, stat_type)
    if mv is None or mv["movement"] == 0:
        return 0.0, ""

    movement  = mv["movement"]
    steaming  = mv["steaming"]
    opening   = mv["opening_line"]
    current   = mv["current_line"]

    # Market moved toward our direction = confirming signal
    if direction == "OVER" and movement > 0:
        # Line went up but we still like OVER — either market agrees it's easy or
        # books protecting themselves; moderate positive signal
        adj  = min(0.03, abs(movement) * 0.04)
        note = f"✅ Line ↑{opening}→{current} (market agrees OVER is soft)"
    elif direction == "UNDER" and movement < 0:
        adj  = min(0.03, abs(movement) * 0.04)
        note = f"✅ Line ↓{opening}→{current} (market agrees UNDER is soft)"
    elif direction == "OVER" and movement < 0:
        # Line dropped — books may know something; negative signal
        adj  = max(-0.04, movement * 0.04)
        note = f"⚠️ Line ↓{opening}→{current} (market moving against OVER)"
    elif direction == "UNDER" and movement > 0:
        # Line rose — market moving against UNDER
        adj  = max(-0.04, -movement * 0.04)
        note = f"⚠️ Line ↑{opening}→{current} (market moving against UNDER)"
    else:
        return 0.0, ""

    if steaming:
        # Strong movement — amplify signal
        adj  = adj * 1.5
        note = note.replace("✅", "🔥").replace("⚠️", "🚨")

    return round(adj, 4), note


# ── Daily summary ─────────────────────────────────────────────────────────────

def movement_summary():
    """Print all props with significant line movement today."""
    history = _load()
    today_str = (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%Y-%m-%d")

    movers = []
    for key, snaps in history.items():
        today_snaps = [s for s in snaps if s.get("ts", "")[:10] == today_str]
        if len(today_snaps) < 2:
            continue
        opening = today_snaps[0]["line"]
        current = today_snaps[-1]["line"]
        movement = round(current - opening, 2)
        if abs(movement) >= 0.25:
            player, stat = key.rsplit("|", 1)
            movers.append((abs(movement), movement, player, stat, opening, current))

    movers.sort(reverse=True)
    if not movers:
        print("No significant line movement today.")
        return

    print(f"\nLine movement today ({today_str}):")
    for _, mv, player, stat, opening, current in movers:
        arrow = "↑" if mv > 0 else "↓"
        print(f"  {player} {stat}: {opening} {arrow} {current} ({mv:+.1f})")


if __name__ == "__main__":
    movement_summary()
