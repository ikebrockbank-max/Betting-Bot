"""
Resolve PrizePicks edge log results using The Odds API scores.

After games finish, checks each logged edge pick against actual box scores
and marks win/loss.

Run after games finish: python3 data/resolve_prizepicks.py
"""

import csv
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import requests
from dotenv import load_dotenv
import os
import pandas as pd

load_dotenv()

EDGES_PATH = Path(__file__).parent.parent / "logs" / "prizepicks_edges.csv"
SCORES_URL = "https://api.the-odds-api.com/v4/sports/basketball_nba/scores"
API_KEY = os.getenv("ODDS_API_KEY")


def get_completed_scores(days_from: int = 1) -> list[dict]:
    """Fetch recently completed NBA game scores from Odds API."""
    params = {
        "apiKey": API_KEY,
        "daysFrom": days_from,
    }
    resp = requests.get(SCORES_URL, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def resolve_prizepicks(dry_run: bool = False) -> None:
    if not EDGES_PATH.exists():
        print("No prizepicks_edges.csv found.")
        return

    df = pd.read_csv(EDGES_PATH, dtype={"result": str, "trade_won": str})

    # Add result/trade_won columns if not present
    if "result" not in df.columns:
        df["result"] = ""
    if "trade_won" not in df.columns:
        df["trade_won"] = ""

    pending = df[df["result"].isna() | (df["result"] == "") | (df["result"] == "nan")]
    print(f"Pending resolution: {len(pending)} picks (of {len(df)} total)")

    if pending.empty:
        print("Nothing to resolve.")
        return

    # Fetch scores
    try:
        scores = get_completed_scores(days_from=3)
    except Exception as e:
        print(f"[error] Could not fetch scores: {e}")
        return

    # Build score lookup: game_id -> {home_team, away_team, scores}
    completed = {}
    for game in scores:
        if game.get("completed"):
            completed[game["id"]] = game

    if not completed:
        print("No completed games found yet.")
        return

    # We can't directly look up player stats from the Odds API scores endpoint
    # (it only returns team scores). Print a notice and the picks to resolve manually
    # for now — a full implementation would need a stats API (e.g. BallDontLie or NBA API).
    print(f"\nFound {len(completed)} completed games.")
    print("NOTE: PrizePicks picks require player box scores to resolve.")
    print("The Odds API only provides team scores. Manual resolution below:\n")

    for idx, row in pending.iterrows():
        print(f"  [{idx}] {row['player']:<22} {row['best_side']:5} {row['pp_line']:5.1f} "
              f"{row['stat_type']:<12}  edge={float(row['best_edge']):.1%}  game={row['game']}")

    print("\nTo manually resolve, enter picks as 'index,w' (win) or 'index,l' (loss).")
    print("Press Enter with no input to skip. Type 'done' when finished.\n")

    if dry_run:
        print("[dry_run] Skipping interactive resolution.")
        return

    updated = 0
    while True:
        try:
            inp = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break
        if inp in ("done", "q", ""):
            break
        try:
            parts = inp.split(",")
            idx = int(parts[0].strip())
            outcome = parts[1].strip()
            if outcome == "w":
                df.at[idx, "result"] = "win"
                df.at[idx, "trade_won"] = 1
                updated += 1
                print(f"  ✓ Marked {df.at[idx, 'player']} as WIN")
            elif outcome == "l":
                df.at[idx, "result"] = "loss"
                df.at[idx, "trade_won"] = 0
                updated += 1
                print(f"  ✗ Marked {df.at[idx, 'player']} as LOSS")
            else:
                print("  Use 'w' for win or 'l' for loss")
        except (IndexError, ValueError):
            print("  Format: index,w or index,l  (e.g. '3,w')")

    if updated > 0:
        df.to_csv(EDGES_PATH, index=False)
        print(f"\nSaved {updated} resolutions to {EDGES_PATH}")
    else:
        print("No changes saved.")


def print_record() -> None:
    """Print win/loss record for all resolved PrizePicks picks."""
    if not EDGES_PATH.exists():
        print("No prizepicks_edges.csv found.")
        return

    df = pd.read_csv(EDGES_PATH, dtype={"result": str, "trade_won": str})
    df["trade_won"] = pd.to_numeric(df["trade_won"], errors="coerce")
    df["best_edge"] = pd.to_numeric(df["best_edge"], errors="coerce")

    resolved = df[df["trade_won"].notna()]
    if resolved.empty:
        print("No resolved picks yet.")
        return

    print(f"\n{'='*50}")
    print(f"PRIZEPICKS RECORD")
    print(f"{'='*50}")
    print(f"  Total resolved : {len(resolved)}")
    print(f"  Win rate       : {resolved['trade_won'].mean():.1%}")
    print(f"  Avg edge       : {resolved['best_edge'].mean():+.1%}")
    print()

    # By stat type
    for stat, group in resolved.groupby("stat_type"):
        wr = group["trade_won"].mean()
        print(f"  {stat:<15} {wr:.0%} ({len(group)} picks)")

    print(f"{'='*50}\n")


if __name__ == "__main__":
    import sys
    if "--record" in sys.argv:
        print_record()
    else:
        resolve_prizepicks()
