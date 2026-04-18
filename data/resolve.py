"""
Automatically resolve edge log results using the Kalshi API.

After markets settle, Kalshi sets market.result = "yes" or "no".
This script checks all unresolved edges and fills in the result + trade_won columns.

Run after games finish: python3 data/resolve.py
"""

import pandas as pd
from pathlib import Path
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from data import kalshi

LOG_PATH = Path(__file__).parent.parent / "logs" / "edges.csv"


def resolve_edges(dry_run: bool = False) -> None:
    df = pd.read_csv(LOG_PATH, dtype={"result": str, "trade_won": str})

    # Only process rows with no result yet
    pending = df[df["result"].isna() | (df["result"] == "")]
    print(f"Pending resolution: {len(pending)} edges (of {len(df)} total)")

    resolved_count = 0

    for idx, row in pending.iterrows():
        ticker = row["kalshi_ticker"]
        best_side = row["best_side"].strip().lower()  # "yes" or "no"

        try:
            market = kalshi.get_market(ticker).get("market", {})
            result = market.get("result", "").strip().lower()

            if result not in ("yes", "no"):
                # Market not yet settled
                continue

            trade_won = 1 if result == best_side else 0

            if not dry_run:
                df.at[idx, "result"] = result
                df.at[idx, "trade_won"] = trade_won

            resolved_count += 1
            status = "WIN" if trade_won else "LOSS"
            print(f"  [{status}] {row['description']} | side={best_side} result={result}")

        except Exception as e:
            print(f"  [error] {ticker}: {e}")

    if not dry_run and resolved_count > 0:
        df.to_csv(LOG_PATH, index=False)
        print(f"\nSaved {resolved_count} resolutions to {LOG_PATH}")
    elif resolved_count == 0:
        print("No new resolutions — markets may not have settled yet.")
    else:
        print(f"\n[dry_run] Would have resolved {resolved_count} edges.")


if __name__ == "__main__":
    resolve_edges()
