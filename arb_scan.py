"""
arb_scan.py — Scheduled prediction market arb scanner.

Scans three types of arb opportunities:
  1. Kalshi internal sweep   — buy all YES outcomes in an event for < $1 total
  2. Kalshi internal ordinal — threshold markets priced inconsistently
  3. Kalshi × PredictIt      — cross-platform binary price gaps
  4. Kalshi × Polymarket     — cross-platform binary price gaps (US-restricted)

Deduplicates against previously-seen arbs and fires push + email alerts.

Run manually:   python3 arb_scan.py
Scheduled:      scheduler.py runs this every ARB_SCAN_INTERVAL_MIN minutes
"""

import json
import sys
from datetime import datetime, UTC
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from scanner_arb import find_arbs, MIN_RAW_EDGE, FEES_POLYMARKET, FEES_PREDICTIT
from scanner_kalshi_internal import scan_internal_arbs
from notify import send_push, send_email, format_arb_alert_email, format_internal_arb_email

SEEN_PATH   = Path("logs/.seen_arb.json")
LOG_PATH    = Path("logs/arb_scan.log")
ARB_LOG_CSV = Path("logs/arb_opportunities.csv")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_seen() -> set:
    if SEEN_PATH.exists():
        try:
            return set(json.loads(SEEN_PATH.read_text()))
        except Exception:
            pass
    return set()


def _save_seen(seen: set):
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps(sorted(seen)))


def _arb_key(a: dict) -> str:
    """Dedup key: counterparty + market IDs + direction."""
    return f"{a.get('counterparty','?')}|{a['kalshi_ticker']}|{a['poly_id']}|{a['arb_type']}"


def _internal_key(a: dict) -> str:
    """Dedup key for internal arbs."""
    if a["arb_type"] == "sweep":
        return f"sweep|{a['event_ticker']}"
    return f"ordinal|{a['event_ticker']}|{a['easy_threshold']}|{a['hard_threshold']}"


def _log(msg: str):
    ts   = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    line = f"[{ts}] {msg}"
    print(line)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def _log_csv(arbs: list[dict]):
    """Append arbs to opportunity CSV for historical tracking."""
    import csv
    fields = [
        "timestamp", "counterparty", "arb_type",
        "kalshi_ticker", "kalshi_title", "poly_question",
        "match_score", "raw_edge", "fee_adj_edge", "profitable",
        "total_cost", "close_time", "kalshi_volume", "poly_volume",
    ]
    write_header = not ARB_LOG_CSV.exists()
    with open(ARB_LOG_CSV, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if write_header:
            w.writeheader()
        ts = datetime.now(UTC).isoformat()
        for a in arbs:
            row = {k: a.get(k, "") for k in fields}
            row["timestamp"] = ts
            w.writerow(row)


# ── Per-platform scan ─────────────────────────────────────────────────────────

def _scan_platform(
    kalshi_markets: list[dict],
    get_fn,
    cp_fees:      dict,
    cp_name:      str,
    seen:         set,
) -> list[dict]:
    """
    Fetch counterparty markets, run arb scan, return NEW arbs not in seen.
    Updates seen in-place.
    """
    try:
        cp_markets = get_fn()
        _log(f"{cp_name.capitalize()}: {len(cp_markets)} markets")
    except Exception as e:
        _log(f"ERROR fetching {cp_name} markets: {e}")
        return []

    try:
        results = find_arbs(
            kalshi_markets, cp_markets,
            cp_fees=cp_fees,
            counterparty=cp_name,
        )
    except Exception as e:
        _log(f"ERROR in {cp_name} arb scan: {e}")
        return []

    all_arbs   = results["arbs"]
    pair_count = results["pair_count"]
    _log(
        f"{cp_name.capitalize()}: matched {pair_count} pairs → "
        f"{len(all_arbs)} arb(s) above {MIN_RAW_EDGE:.0%} raw edge"
    )

    new_arbs = [a for a in all_arbs if _arb_key(a) not in seen]
    for a in new_arbs:
        seen.add(_arb_key(a))

    return new_arbs


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    _log("=== Arb scan started ===")

    # ── Fetch Kalshi (shared for all platform scans) ──────────────────────────
    try:
        from data.kalshi import get_open_prediction_markets
        kalshi_markets = get_open_prediction_markets()
        _log(f"Kalshi: {len(kalshi_markets)} open prediction markets")
    except Exception as e:
        _log(f"ERROR fetching Kalshi markets: {e}")
        return

    seen = _load_seen()
    all_new_arbs: list[dict] = []

    # ── Internal Kalshi arb scan (sweep + ordinal) ────────────────────────────
    try:
        internal = scan_internal_arbs(kalshi_markets)
        total_internal = internal["total"]
        _log(f"Internal: {len(internal['sweep_arbs'])} sweep arbs, "
             f"{len(internal['ordinal_arbs'])} ordinal inversions")

        new_internal = []
        for a in internal["sweep_arbs"] + internal["ordinal_arbs"]:
            k = _internal_key(a)
            if k not in seen:
                seen.add(k)
                new_internal.append(a)

        if new_internal:
            # Push for internal arbs
            top = new_internal[0]
            if top["arb_type"] == "sweep":
                push_msg = (
                    f"[Internal] {top['event_ticker']} sweep: "
                    f"{top['market_count']} markets @ {top['total_cost_cents']:.0f}¢ total → "
                    f"{top['raw_edge_pct']} edge"
                )
            else:
                push_msg = (
                    f"[Internal] Ordinal inversion: {top['event_ticker']} | "
                    f"{top['raw_edge_pct']} mispricing"
                )
            extra = f" +{len(new_internal)-1} more" if len(new_internal) > 1 else ""
            send_push(push_msg + extra, title="🎯 Kalshi Internal Arb!")
            _log(f"Push sent (internal): {push_msg[:80]}")

            try:
                subject, html, plain = format_internal_arb_email(new_internal)
                send_email(subject, html, plain)
                _log(f"Email sent: {subject}")
            except Exception as e:
                _log(f"Email error (non-fatal): {e}")

    except Exception as e:
        _log(f"ERROR in internal arb scan: {e}")

    # ── PredictIt scan (primary — US-legal, federally regulated) ─────────────
    from data.predictit import get_markets as get_pi
    pi_arbs = _scan_platform(
        kalshi_markets, get_pi,
        cp_fees=FEES_PREDICTIT, cp_name="predictit", seen=seen,
    )
    all_new_arbs.extend(pi_arbs)

    # ── Polymarket scan (secondary — best for signal even if US-restricted) ──
    from data.polymarket import get_markets as get_poly
    poly_arbs = _scan_platform(
        kalshi_markets, get_poly,
        cp_fees=FEES_POLYMARKET, cp_name="polymarket", seen=seen,
    )
    all_new_arbs.extend(poly_arbs)

    _save_seen(seen)

    if not all_new_arbs:
        _log("Scan complete — no new arb opportunities.")
        return

    # Sort by raw edge descending across platforms
    all_new_arbs.sort(key=lambda a: -a["raw_edge"])

    # ── Log ───────────────────────────────────────────────────────────────────
    profitable = [a for a in all_new_arbs if a["profitable"]]
    watching   = [a for a in all_new_arbs if not a["profitable"]]

    _log(f"🚨 {len(all_new_arbs)} NEW ARB(S): {len(profitable)} profitable, {len(watching)} watching")
    for a in all_new_arbs[:10]:
        flag = "✅" if a["profitable"] else "👀"
        cp   = a.get("counterparty", "?").upper()
        _log(
            f"  {flag} [{a['match_score']:.2f}] [{cp}] {a['kalshi_title'][:45]} | "
            f"raw={a['raw_edge_pct']} fee_adj={a['fee_adj_pct']}"
        )

    _log_csv(all_new_arbs)

    # ── Push notification ─────────────────────────────────────────────────────
    if profitable:
        top = profitable[0]
        cp_label = top.get("counterparty", "").upper()
        push_msg = (
            f"[Kalshi×{cp_label}] {top['kalshi_title'][:40]} | "
            f"{top['raw_edge_pct']} raw / {top['fee_adj_pct']} net"
        )
        if len(profitable) > 1:
            push_msg += f" +{len(profitable)-1} more"
        send_push(push_msg, title="🎯 Prediction Market Arb!")
        _log(f"Push sent: {push_msg[:80]}")
    elif watching:
        top = watching[0]
        cp_label = top.get("counterparty", "").upper()
        push_msg = (
            f"[Kalshi×{cp_label}] {top['kalshi_title'][:40]} | "
            f"{top['raw_edge_pct']} raw (below fee threshold)"
        )
        send_push(push_msg, title="👀 Arb Watch List")
        _log(f"Push sent (watch): {push_msg[:80]}")

    # ── Email ─────────────────────────────────────────────────────────────────
    try:
        subject, html, plain = format_arb_alert_email(all_new_arbs)
        send_email(subject, html, plain)
        _log(f"Email sent: {subject}")
    except Exception as e:
        _log(f"Email error (non-fatal): {e}")

    _log(f"=== Arb scan complete — {len(all_new_arbs)} new arb(s) alerted ===")


if __name__ == "__main__":
    run()
