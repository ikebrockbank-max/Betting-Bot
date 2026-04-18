"""
Time-series edge analysis.

Answers: do price gaps between Kalshi and sportsbooks grow near game time?
Which direction does each market move — toward or away from fair value?

Requires: logs/price_history.csv (built by watch.py over multiple scans)

Run: python3 models/time_analysis.py
"""

import pandas as pd
from pathlib import Path

PRICE_HISTORY_PATH = Path(__file__).parent.parent / "logs" / "price_history.csv"


def load() -> pd.DataFrame:
    df = pd.read_csv(PRICE_HISTORY_PATH, parse_dates=["timestamp"])
    df["edge_vs_ask"] = df["edge_vs_ask"].astype(float)
    df["edge_vs_bid"] = df["edge_vs_bid"].astype(float)
    df["best_edge"] = df[["edge_vs_ask", "edge_vs_bid"]].max(axis=1)
    # Round timestamps to nearest 5 minutes to group all markets in a scan together
    df["scan_time"] = df["timestamp"].dt.floor("5min")
    return df


def edge_over_time(df: pd.DataFrame) -> None:
    """Show average edge magnitude per scan (grouped by 5-min window)."""
    print("\n── AVERAGE EDGE BY SCAN ──")
    by_time = (
        df.groupby("scan_time")["best_edge"]
        .agg(["mean", "max", "count"])
        .reset_index()
        .sort_values("scan_time")
    )
    for _, row in by_time.iterrows():
        bar = "█" * int(max(0, row["mean"]) * 200)
        print(f"  {row['scan_time'].strftime('%m-%d %H:%M')}  "
              f"avg={row['mean']:+.2%}  max={row['max']:+.2%}  n={int(row['count'])}  {bar}")


def biggest_movers(df: pd.DataFrame, top_n: int = 10) -> None:
    """Markets with largest edge change across scans."""
    print(f"\n── TOP {top_n} BIGGEST EDGE MOVERS ──")
    market_stats = (
        df.groupby("kalshi_ticker")["best_edge"]
        .agg(first="first", last="last", max="max", count="count")
        .assign(movement=lambda x: x["last"] - x["first"])
        .sort_values("movement", ascending=False)
        .head(top_n)
        .reset_index()
    )
    for _, row in market_stats.iterrows():
        direction = "▲" if row["movement"] > 0 else "▼"
        desc = df[df["kalshi_ticker"] == row["kalshi_ticker"]]["description"].iloc[0][:40]
        print(f"  {direction} {desc:<40} "
              f"first={row['first']:+.2%} → last={row['last']:+.2%}  "
              f"Δ={row['movement']:+.2%}  scans={int(row['count'])}")


def edges_above_threshold(df: pd.DataFrame, threshold: float = 0.06) -> None:
    """Show when/if any markets crossed the 6% edge threshold."""
    above = df[df["best_edge"] >= threshold]
    print(f"\n── MARKETS THAT CROSSED {threshold:.0%} EDGE THRESHOLD ──")
    if above.empty:
        print("  None yet.")
        return
    for ticker, group in above.groupby("kalshi_ticker"):
        desc = group["description"].iloc[0][:40]
        times = group["timestamp"].dt.strftime("%H:%M").tolist()
        edges = [f"{e:.1%}" for e in group["best_edge"].tolist()]
        print(f"  {desc}")
        print(f"    Times: {', '.join(times)}")
        print(f"    Edges: {', '.join(edges)}")


def print_report() -> None:
    df = load()
    n_markets = df["kalshi_ticker"].nunique()
    n_scans = df["scan_time"].nunique()
    print(f"\n{'='*60}")
    print(f"TIME SERIES ANALYSIS")
    print(f"{'='*60}")
    print(f"  Scans recorded : {n_scans}")
    print(f"  Unique markets : {n_markets}")
    print(f"  Date range     : {df['timestamp'].min().strftime('%m-%d %H:%M')} → "
          f"{df['timestamp'].max().strftime('%m-%d %H:%M')}")

    edge_over_time(df)
    biggest_movers(df)
    edges_above_threshold(df)
    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    print_report()
