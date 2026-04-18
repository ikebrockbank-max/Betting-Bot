"""
Edge calculator for Kalshi sports props.

Compares Kalshi market prices to vig-adjusted sportsbook consensus
and logs opportunities that clear the minimum tradable threshold.
"""

import csv
import math
from dataclasses import dataclass, asdict
from datetime import datetime, UTC
from pathlib import Path

# ── Thresholds ────────────────────────────────────────────────────────────────
KALSHI_FEE = 0.02
SLIPPAGE_BUFFER = 0.02
MODEL_ERROR_BUFFER = 0.02
MIN_EDGE = KALSHI_FEE + SLIPPAGE_BUFFER + MODEL_ERROR_BUFFER  # 6%

# Liquidity tiers
LIQUIDITY_IGNORE = 50      # below this: skip entirely
LIQUIDITY_WEAK   = 300     # 50–300: weak signal
# 300+: valid signal

LOG_PATH = Path(__file__).parent.parent / "logs" / "edges.csv"


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class EdgeOpportunity:
    timestamp: str
    kalshi_ticker: str
    description: str
    group_id: str            # player+game for correlation grouping (e.g. "BALL1_MIACHA")
    kalshi_yes_ask: float
    kalshi_yes_bid: float
    kalshi_size_ask: float
    liquidity_tier: str      # "ignore" | "weak" | "valid"
    fair_prob: float
    edge_vs_ask: float       # YES edge (fair_prob - ask)
    edge_vs_bid: float       # NO edge  ((1-fair_prob) - no_ask)
    best_edge: float
    best_side: str           # "YES" or "NO"
    edge_quality: float      # best_edge * min(size, 1000) — penalizes thin markets
    clears_threshold: bool
    books_used: int
    notes: str = ""
    result: str = ""         # filled by resolve.py: "yes" or "no"
    trade_won: str = ""      # filled by resolve.py: "1" win, "0" loss, "" pending


def liquidity_tier(size: float) -> str:
    if size < LIQUIDITY_IGNORE:
        return "ignore"
    elif size < LIQUIDITY_WEAK:
        return "weak"
    return "valid"


# ── Core calculations ─────────────────────────────────────────────────────────

def poisson_over_prob(mean: float, threshold: int) -> float:
    """P(X >= threshold) for Poisson(mean). Used for line mismatch adjustment."""
    prob_under = sum(
        (math.exp(-mean) * mean**k) / math.factorial(k)
        for k in range(threshold)
    )
    return 1 - prob_under


def compute_edge(
    kalshi_yes_ask: float,
    kalshi_yes_bid: float,
    fair_prob: float,
) -> tuple[float, float, str]:
    """
    Returns (edge_vs_ask, edge_vs_bid, best_side).
    edge_vs_ask > 0 → buying YES is +EV
    edge_vs_bid > 0 → buying NO is +EV
    """
    edge_yes = fair_prob - kalshi_yes_ask
    edge_no = (1 - fair_prob) - (1 - kalshi_yes_bid)
    best_side = "YES" if edge_yes >= edge_no else "NO"
    return edge_yes, edge_no, best_side


# ── Logging ───────────────────────────────────────────────────────────────────

def _ensure_log():
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not LOG_PATH.exists():
        with open(LOG_PATH, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(EdgeOpportunity.__dataclass_fields__.keys()))
            writer.writeheader()


def log_opportunity(opp: EdgeOpportunity):
    _ensure_log()
    with open(LOG_PATH, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(EdgeOpportunity.__dataclass_fields__.keys()))
        writer.writerow(asdict(opp))


# ── Main evaluation function ──────────────────────────────────────────────────

def evaluate_market(
    kalshi_ticker: str,
    description: str,
    kalshi_yes_ask: float,
    kalshi_yes_bid: float,
    kalshi_ask_size: float,
    fair_prob: float,
    group_id: str = "",
    books_used: int = 1,
    notes: str = "",
) -> EdgeOpportunity | None:
    """
    Evaluate a single Kalshi market against a fair probability estimate.
    Returns None (and does not log) if liquidity is below LIQUIDITY_IGNORE.
    """
    tier = liquidity_tier(kalshi_ask_size)
    if tier == "ignore":
        return None

    edge_yes, edge_no, best_side = compute_edge(kalshi_yes_ask, kalshi_yes_bid, fair_prob)
    best_edge = edge_yes if best_side == "YES" else edge_no
    quality = round(best_edge * min(kalshi_ask_size, 1000), 2)
    clears = best_edge >= MIN_EDGE

    opp = EdgeOpportunity(
        timestamp=datetime.now(UTC).isoformat(),
        kalshi_ticker=kalshi_ticker,
        description=description,
        group_id=group_id,
        kalshi_yes_ask=kalshi_yes_ask,
        kalshi_yes_bid=kalshi_yes_bid,
        kalshi_size_ask=kalshi_ask_size,
        liquidity_tier=tier,
        fair_prob=fair_prob,
        edge_vs_ask=edge_yes,
        edge_vs_bid=edge_no,
        best_edge=best_edge,
        best_side=best_side,
        edge_quality=quality,
        clears_threshold=clears,
        books_used=books_used,
        notes=notes,
    )

    log_opportunity(opp)

    if clears:
        tier_flag = "" if tier == "valid" else f" [{tier}]"
        print(
            f"[EDGE{tier_flag}] {description}\n"
            f"  ask={kalshi_yes_ask:.2%} fair={fair_prob:.2%} "
            f"edge={best_edge:.2%} side={best_side} size=${kalshi_ask_size:.0f} quality={quality:.1f}"
        )
    return opp
