"""
Evaluate whether detected edges are real.

Loads edges.csv (resolved entries only) and computes:
  - Overall calibration: does our model probability match actual outcomes?
  - Edge bucket analysis: do higher edges produce higher win rates?
  - EV tracking: what would $1/trade have returned?
  - Time-to-decay: how long did edges persist? (requires scanner timestamps)

Run: python3 models/evaluate.py
"""

import pandas as pd
from pathlib import Path

LOG_PATH = Path(__file__).parent.parent / "logs" / "edges.csv"

EDGE_BUCKETS = [
    (0.06, 0.08, "6–8%"),
    (0.08, 0.10, "8–10%"),
    (0.10, 0.15, "10–15%"),
    (0.15, 1.00, "15%+"),
]


def load_resolved(path: Path = LOG_PATH) -> pd.DataFrame:
    df = pd.read_csv(path)
    resolved = df[df["trade_won"].notna() & (df["trade_won"] != "")]
    resolved = resolved.copy()
    resolved["trade_won"] = resolved["trade_won"].astype(int)
    resolved["best_edge"] = resolved["best_edge"].astype(float)
    resolved["fair_prob"] = resolved["fair_prob"].astype(float)
    resolved["kalshi_yes_ask"] = resolved["kalshi_yes_ask"].astype(float)
    resolved["kalshi_size_ask"] = resolved["kalshi_size_ask"].astype(float)
    return resolved


def ev_per_trade(row: pd.Series) -> float:
    """
    Expected value of a $1 bet on our side.
    YES trade: win (1 - ask) with prob fair_prob, lose ask with prob (1 - fair_prob)
    NO trade:  win (1 - no_ask) with prob (1-fair_prob), lose no_ask with prob fair_prob
    """
    if row["best_side"] == "YES":
        win_payout = 1 - row["kalshi_yes_ask"]
        loss_cost = row["kalshi_yes_ask"]
        return row["fair_prob"] * win_payout - (1 - row["fair_prob"]) * loss_cost
    else:
        no_ask = 1 - row["kalshi_yes_bid"] if "kalshi_yes_bid" in row else 1 - row["kalshi_yes_ask"]
        fair_no = 1 - row["fair_prob"]
        win_payout = 1 - no_ask
        loss_cost = no_ask
        return fair_no * win_payout - (1 - fair_no) * loss_cost


def overall_stats(df: pd.DataFrame) -> dict:
    win_rate = df["trade_won"].mean()
    expected = df["fair_prob"].mean()
    avg_ev = df.apply(ev_per_trade, axis=1).mean()
    return {
        "total_resolved": len(df),
        "win_rate":        round(win_rate, 4),
        "expected_prob":   round(expected, 4),
        "calibration_gap": round(win_rate - expected, 4),
        "avg_ev_per_trade": round(avg_ev, 4),
    }


def bucket_analysis(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for low, high, label in EDGE_BUCKETS:
        bucket = df[(df["best_edge"] >= low) & (df["best_edge"] < high)]
        if bucket.empty:
            continue
        win_rate = bucket["trade_won"].mean()
        expected = bucket["fair_prob"].mean()
        avg_size = bucket["kalshi_size_ask"].mean()
        total_ev = bucket.apply(ev_per_trade, axis=1).sum()
        rows.append({
            "edge_range":      label,
            "count":           len(bucket),
            "avg_edge":        round(bucket["best_edge"].mean(), 3),
            "win_rate":        round(win_rate, 3),
            "expected_prob":   round(expected, 3),
            "edge_vs_reality": round(win_rate - expected, 3),
            "avg_size_$":      round(avg_size, 0),
            "total_ev_$1_bets": round(total_ev, 3),
        })
    return pd.DataFrame(rows)


def calibration_verdict(gap: float) -> str:
    if abs(gap) < 0.03:
        return "NEUTRAL — model roughly calibrated, edge may be real but small"
    elif gap > 0.03:
        return "GOOD — outperforming model expectations, edge looks real"
    else:
        return "BAD  — underperforming, model is overconfident or edge is fake"


def print_report(df: pd.DataFrame) -> None:
    print("\n" + "=" * 60)
    print("EDGE PERFORMANCE REPORT")
    print("=" * 60)

    stats = overall_stats(df)
    print("\n── OVERALL ──")
    for k, v in stats.items():
        print(f"  {k:<25} {v}")
    print(f"\n  verdict: {calibration_verdict(stats['calibration_gap'])}")

    print("\n── BY EDGE BUCKET ──")
    buckets = bucket_analysis(df)
    if buckets.empty:
        print("  Not enough resolved trades to bucket yet.")
    else:
        print(buckets.to_string(index=False))

    print("\n── WHAT TO LOOK FOR ──")
    print("  win_rate > expected_prob  →  model is predictive")
    print("  higher edge → higher win_rate  →  signal is real")
    print("  total_ev_$1_bets > 0  →  would have been profitable")
    print("=" * 60)


if __name__ == "__main__":
    df = load_resolved()
    if df.empty:
        print("No resolved edges yet. Run data/resolve.py after games finish.")
    else:
        print_report(df)
