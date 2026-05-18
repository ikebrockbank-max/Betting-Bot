"""
scheduler.py — persistent cloud loop that runs auto_scan every N minutes.

Deploy this on Railway / Render / any VPS:
  python3 scheduler.py

Set SCAN_INTERVAL_MIN in your environment to change frequency (default: 15).
"""

import os
import sys
import time
import traceback
from datetime import datetime, UTC
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

SCAN_INTERVAL_MIN          = int(os.getenv("SCAN_INTERVAL_MIN",          "15"))
ARB_SCAN_INTERVAL_MIN      = int(os.getenv("ARB_SCAN_INTERVAL_MIN",      "30"))  # cross-platform arb
INTERNAL_ARB_INTERVAL_MIN  = int(os.getenv("INTERNAL_ARB_INTERVAL_MIN",  "15"))  # Kalshi internal arb
INJURY_POLL_INTERVAL_SEC   = int(os.getenv("INJURY_POLL_INTERVAL_SEC",   "120")) # injury check (seconds)
PARLAY_SCAN_INTERVAL_MIN   = int(os.getenv("PARLAY_SCAN_INTERVAL_MIN",   "30"))  # PP parlay builder
PP_REPORT_INTERVAL_MIN     = int(os.getenv("PP_REPORT_INTERVAL_MIN",     "30"))  # pre-game PP parlay report
DAILY_DIGEST_HOUR_UTC      = int(os.getenv("DAILY_DIGEST_HOUR_UTC",      "15"))  # daily digest hour (UTC)


def log(msg: str):
    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}", flush=True)


def main():
    log(f"Scheduler started — sports scan every {SCAN_INTERVAL_MIN}m | "
        f"internal arb every {INTERNAL_ARB_INTERVAL_MIN}m | "
        f"cross-platform arb every {ARB_SCAN_INTERVAL_MIN}m | "
        f"injury check every {INJURY_POLL_INTERVAL_SEC}s | "
        f"PP parlay every {PARLAY_SCAN_INTERVAL_MIN}m | "
        f"PP pre-game report every {PP_REPORT_INTERVAL_MIN}m")

    import importlib
    last_arb_scan      = 0.0
    last_kalshi_scan   = 0.0
    last_internal_scan = 0.0
    last_injury_check  = 0.0
    last_parlay_scan   = 0.0
    last_pp_report     = 0.0
    last_daily_digest  = ""     # date string "YYYY-MM-DD" of last digest
    injury_seen: dict  = {}     # in-memory dedup across iterations
    pp_injury_seen: dict = {}   # in-memory dedup for PP injury alerts

    while True:
        # ── PP / ParlayPlay / Underdog / consensus scan (every 15 min) ───────
        try:
            import auto_scan
            importlib.reload(auto_scan)
            auto_scan.run()
        except Exception:
            log(f"ERROR during sports scan:\n{traceback.format_exc()}")

        # ── Kalshi NBA props vs sportsbook scan (every 15 min) ────────────────
        now = time.time()
        if now - last_kalshi_scan >= SCAN_INTERVAL_MIN * 60:
            try:
                from scanner import scan_nba_markets
                import scanner as _scanner_mod
                importlib.reload(_scanner_mod)
                _scanner_mod.scan_nba_markets()
                last_kalshi_scan = time.time()
            except Exception:
                log(f"ERROR during Kalshi NBA scan:\n{traceback.format_exc()}")

        # ── Kalshi internal arb scan (every INTERNAL_ARB_INTERVAL_MIN minutes) ─
        now = time.time()
        if now - last_internal_scan >= INTERNAL_ARB_INTERVAL_MIN * 60:
            try:
                from data.kalshi import get_open_prediction_markets
                from scanner_kalshi_internal import scan_internal_arbs
                from notify import send_push, send_email, format_internal_arb_email
                import json
                from pathlib import Path
                importlib.reload(__import__("scanner_kalshi_internal"))
                markets = get_open_prediction_markets()
                results = scan_internal_arbs(markets)
                seen_path = Path("logs/.seen_arb.json")
                seen = set(json.loads(seen_path.read_text())) if seen_path.exists() else set()
                new_arbs = []
                for a in results["sweep_arbs"] + results["ordinal_arbs"]:
                    k = f"internal|{a['arb_type']}|{a.get('event_ticker','')}"
                    if k not in seen:
                        seen.add(k)
                        new_arbs.append(a)
                if new_arbs:
                    subject, html, plain = format_internal_arb_email(new_arbs)
                    send_email(subject, html, plain)
                    send_push(f"{len(new_arbs)} new internal Kalshi arb(s)!", title="🎯 Kalshi Internal Arb")
                last_internal_scan = time.time()
            except Exception:
                log(f"ERROR during internal arb scan:\n{traceback.format_exc()}")

        # ── Prediction market arb scan (every ARB_SCAN_INTERVAL_MIN minutes) ─
        now = time.time()
        if now - last_arb_scan >= ARB_SCAN_INTERVAL_MIN * 60:
            try:
                import arb_scan
                importlib.reload(arb_scan)
                arb_scan.run()
                last_arb_scan = time.time()
            except Exception:
                log(f"ERROR during arb scan:\n{traceback.format_exc()}")

        # ── Injury speed alert (every INJURY_POLL_INTERVAL_SEC seconds) ──────────
        now = time.time()
        if now - last_injury_check >= INJURY_POLL_INTERVAL_SEC:
            try:
                import injury_alert
                importlib.reload(injury_alert)
                injury_seen = injury_alert.run_once(injury_seen)
                last_injury_check = time.time()
            except Exception:
                log(f"ERROR during injury check:\n{traceback.format_exc()}")

        # ── PP injury alert (every INJURY_POLL_INTERVAL_SEC seconds) ──────────
        now = time.time()
        if now - last_injury_check >= INJURY_POLL_INTERVAL_SEC:
            try:
                import pp_injury_alert
                importlib.reload(pp_injury_alert)
                pp_injury_seen = pp_injury_alert.run_once(pp_injury_seen)
            except Exception:
                log(f"ERROR during PP injury check:\n{traceback.format_exc()}")

        # ── PP parlay builder (every PARLAY_SCAN_INTERVAL_MIN minutes) ────────
        now = time.time()
        if now - last_parlay_scan >= PARLAY_SCAN_INTERVAL_MIN * 60:
            try:
                import scanner_pp_parlay
                importlib.reload(scanner_pp_parlay)
                scanner_pp_parlay.run()
                last_parlay_scan = time.time()
            except Exception:
                log(f"ERROR during PP parlay scan:\n{traceback.format_exc()}")

        # ── Pre-game PP parlay report — fires ~3hr before tip-off (every 30m) ─
        now = time.time()
        if now - last_pp_report >= PP_REPORT_INTERVAL_MIN * 60:
            try:
                import pp_playoff_report
                importlib.reload(pp_playoff_report)
                pp_playoff_report.run()
                last_pp_report = time.time()
            except Exception:
                log(f"ERROR during PP pre-game report:\n{traceback.format_exc()}")

        # ── Daily digest — once per day at DAILY_DIGEST_HOUR_UTC ─────────────
        now_dt = datetime.now(UTC)
        today  = now_dt.strftime("%Y-%m-%d")
        if now_dt.hour >= DAILY_DIGEST_HOUR_UTC and last_daily_digest != today:
            try:
                import daily_digest
                importlib.reload(daily_digest)
                daily_digest.run()
                last_daily_digest = today
            except Exception:
                log(f"ERROR during daily digest:\n{traceback.format_exc()}")

        log(f"Sleeping {SCAN_INTERVAL_MIN}m until next scan...")
        time.sleep(SCAN_INTERVAL_MIN * 60)


if __name__ == "__main__":
    main()
