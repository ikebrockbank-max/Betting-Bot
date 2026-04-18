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

SCAN_INTERVAL_MIN = int(os.getenv("SCAN_INTERVAL_MIN", "15"))


def log(msg: str):
    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}", flush=True)


def main():
    log(f"Scheduler started — scanning every {SCAN_INTERVAL_MIN} minutes")

    # Run immediately on start, then on interval
    while True:
        try:
            import auto_scan
            # Reload module each run so code changes are picked up
            import importlib
            importlib.reload(auto_scan)
            auto_scan.run()
        except Exception:
            log(f"ERROR during scan:\n{traceback.format_exc()}")

        next_run = SCAN_INTERVAL_MIN * 60
        log(f"Sleeping {SCAN_INTERVAL_MIN}m until next scan...")
        time.sleep(next_run)


if __name__ == "__main__":
    main()
