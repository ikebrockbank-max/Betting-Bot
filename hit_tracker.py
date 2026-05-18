"""
hit_tracker.py — PP pick logging and hit-rate tracker.

When pp_playoff_report generates picks, log them to logs/pick_log.csv.
Each day, check yesterday's picks against actual game results to
compute hit rate stats.

Columns: date, game_date, player, stat_type, line, direction, prob,
         result (actual value), hit (1/0)

Export functions:
  log_picks(picks: list[dict], game_date: str)
  resolve_yesterday_picks()
  get_summary() -> dict
"""

import csv
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from data.nba_stats import get_player_stats

LOG_PATH = Path("logs/pick_log.csv")
LOG_PATH_STR = str(LOG_PATH)

FIELDNAMES = ["date", "game_date", "player", "stat_type", "line",
              "direction", "prob", "result", "hit"]

LOOKBACK_DAYS = 30   # days to include in summary stats


def _ensure_log():
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not LOG_PATH.exists():
        with open(LOG_PATH, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writeheader()


def _read_rows() -> list[dict]:
    _ensure_log()
    with open(LOG_PATH, newline="") as f:
        return list(csv.DictReader(f))


def _write_rows(rows: list[dict]):
    _ensure_log()
    with open(LOG_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def log_picks(picks: list[dict], game_date: str):
    """
    Append picks to the CSV log (skips picks already logged for that game_date).
    picks: list of dicts with keys player, stat_type, line, direction, prob
    game_date: "YYYY-MM-DD" string
    """
    try:
        _ensure_log()
        existing_rows = _read_rows()

        # Build set of (game_date, player, stat_type) already logged
        already = {
            (r["game_date"], r["player"].lower(), r["stat_type"])
            for r in existing_rows
        }

        logged_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        new_rows = []
        for pick in picks:
            key = (game_date, pick["player"].lower(), pick["stat_type"])
            if key in already:
                continue
            new_rows.append({
                "date":      logged_date,
                "game_date": game_date,
                "player":    pick["player"],
                "stat_type": pick["stat_type"],
                "line":      pick["line"],
                "direction": pick["direction"],
                "prob":      pick["prob"],
                "result":    "",
                "hit":       "",
            })
            already.add(key)

        if new_rows:
            with open(LOG_PATH, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
                writer.writerows(new_rows)
            print(f"[hit_tracker] Logged {len(new_rows)} picks for {game_date}")
        else:
            print(f"[hit_tracker] No new picks to log for {game_date}")
    except Exception as e:
        print(f"[hit_tracker] log_picks error (non-fatal): {e}")


def resolve_yesterday_picks():
    """
    Check picks from yesterday. Uses get_player_stats() to find the most
    recent game result and compare against the line.
    Updates result and hit columns in the CSV.
    """
    try:
        rows = _read_rows()
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

        to_resolve = [
            (i, r) for i, r in enumerate(rows)
            if r["game_date"] == yesterday and r["hit"] == ""
        ]

        if not to_resolve:
            print(f"[hit_tracker] Nothing to resolve for {yesterday}")
            return

        print(f"[hit_tracker] Resolving {len(to_resolve)} picks from {yesterday}...")
        updated = 0

        for i, row in to_resolve:
            try:
                stats = get_player_stats(row["player"], row["stat_type"])
                if not stats:
                    continue

                last_5 = stats.get("last_5", [])
                if not last_5:
                    continue

                # Most recent game is first in last_5
                actual = float(last_5[0])
                line   = float(row["line"])

                if row["direction"] == "OVER":
                    hit = 1 if actual > line else 0
                else:
                    hit = 1 if actual < line else 0

                rows[i]["result"] = actual
                rows[i]["hit"]    = hit
                updated += 1
            except Exception as e:
                print(f"[hit_tracker] resolve error for {row['player']} {row['stat_type']}: {e}")
                continue

        _write_rows(rows)
        print(f"[hit_tracker] Resolved {updated} of {len(to_resolve)} picks")
    except Exception as e:
        print(f"[hit_tracker] resolve_yesterday_picks error (non-fatal): {e}")


def get_summary() -> dict:
    """
    Returns summary stats for picks logged in the last LOOKBACK_DAYS days.

    {
      total: int,
      hits: int,
      hit_rate: float,
      by_stat: {
        stat_type: {total: int, hits: int, rate: float}
      }
    }
    """
    try:
        rows = _read_rows()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")

        resolved = [
            r for r in rows
            if r["game_date"] >= cutoff and r["hit"] != ""
        ]

        total = len(resolved)
        hits  = sum(int(r["hit"]) for r in resolved)

        by_stat: dict[str, dict] = {}
        for r in resolved:
            stat = r["stat_type"]
            if stat not in by_stat:
                by_stat[stat] = {"total": 0, "hits": 0, "rate": 0.0}
            by_stat[stat]["total"] += 1
            by_stat[stat]["hits"]  += int(r["hit"])

        for stat, d in by_stat.items():
            d["rate"] = round(d["hits"] / d["total"], 3) if d["total"] > 0 else 0.0

        return {
            "total":    total,
            "hits":     hits,
            "hit_rate": round(hits / total, 3) if total > 0 else 0.0,
            "by_stat":  by_stat,
        }
    except Exception as e:
        print(f"[hit_tracker] get_summary error (non-fatal): {e}")
        return {"total": 0, "hits": 0, "hit_rate": 0.0, "by_stat": {}}


if __name__ == "__main__":
    resolve_yesterday_picks()
    summary = get_summary()
    print(f"Summary: {summary['total']} picks, {summary['hit_rate']:.1%} hit rate")
    for stat, d in summary.get("by_stat", {}).items():
        print(f"  {stat}: {d['hits']}/{d['total']} ({d['rate']:.1%})")
