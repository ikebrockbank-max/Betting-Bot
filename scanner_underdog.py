"""
scanner_underdog.py — Underdog Fantasy bug detector.

Detects two types of mispriced alternate lines:

  easy_alternate   — alternate value < balanced AND higher_mult >= 0.55
                     AND (balanced - alternate) >= 0.5.
                     You get a boosted multiplier on a threshold easier than standard.

  expiring_line    — expires_at is non-null and within 4 hours from now.
                     Flash-sale urgency — act before the line disappears.

Run:
  python3 scanner_underdog.py              # all sports
  python3 scanner_underdog.py --sport NBA  # filter by sport_id (case-insensitive)
  python3 scanner_underdog.py --all-lines  # show all grouped lines too
  python3 scanner_underdog.py --auto       # deduplicate + send alerts
"""

import argparse
import csv
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from data.underdog import get_grouped_lines

# ── Paths ─────────────────────────────────────────────────────────────────────
LOGS_DIR = Path("logs")
UD_BUGS_LOG = LOGS_DIR / "ud_bugs.csv"
SEEN_UD_BUGS_PATH = LOGS_DIR / ".seen_ud_bugs.json"

UD_CSV_FIELDS = [
    "timestamp", "sport", "player", "stat",
    "balanced_line", "alt_line", "alt_multiplier", "gap", "bug_type",
]

# Thresholds
_EASY_ALT_MIN_MULT = 0.55
_EASY_ALT_MIN_GAP = 0.5
_EXPIRING_HOURS = 4


# ── Bug detection ─────────────────────────────────────────────────────────────

def _detect_bugs(grouped: dict) -> list[dict]:
    """Run both detectors over the grouped lines. Returns list of bug dicts."""
    now = datetime.now(timezone.utc)
    expiry_cutoff = now + timedelta(hours=_EXPIRING_HOURS)
    bugs: list[dict] = []

    for (name, stat, sport), entry in grouped.items():
        balanced = entry.get("balanced")
        alternates = entry.get("alternates", [])
        expires_at = entry.get("expires_at")

        base = {
            "player": name,
            "stat": stat,
            "sport": sport,
            "balanced": balanced,
            "expires_at": expires_at,
        }

        # easy_alternate: alternate below balanced, good multiplier, meaningful gap
        if balanced is not None:
            for alt_value, higher_mult in alternates:
                gap = round(balanced - alt_value, 4)
                if alt_value < balanced and higher_mult >= _EASY_ALT_MIN_MULT and gap >= _EASY_ALT_MIN_GAP:
                    bugs.append({
                        **base,
                        "alt_value": alt_value,
                        "alt_mult": higher_mult,
                        "gap": gap,
                        "bug_type": "easy_alternate",
                    })

        # expiring_line: non-null expires_at within the next 4 hours
        if expires_at:
            try:
                exp_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                if now <= exp_dt <= expiry_cutoff:
                    # Use balanced as alt_value placeholder; report balanced line
                    bugs.append({
                        **base,
                        "alt_value": balanced if balanced is not None else 0.0,
                        "alt_mult": 0.0,
                        "gap": 0.0,
                        "bug_type": "expiring_line",
                    })
            except (ValueError, TypeError):
                pass

    # Sort easy_alternate by multiplier desc (biggest edge first), then expiring
    bugs.sort(key=lambda b: (-b["alt_mult"] if b["bug_type"] == "easy_alternate" else 0, b["player"]))
    return bugs


# ── Output helpers ─────────────────────────────────────────────────────────────

def _print_bugs(bugs: list[dict], label: str = ""):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    header = f"UNDERDOG FANTASY BUG SCAN{' — ' + label if label else ''}"
    print(f"\n{'='*65}")
    print(header)
    print(f"{'='*65}")
    print(f"  Scan time: {ts}")

    easy = [b for b in bugs if b["bug_type"] == "easy_alternate"]
    expiring = [b for b in bugs if b["bug_type"] == "expiring_line"]

    if easy:
        print(f"\n  EXPLOITABLE ALTERNATES ({len(easy)}) — easier than standard with boosted payout:")
        for b in easy:
            print(
                f"\n    * [{b['sport']}] {b['player']} — {b['stat']}"
                f"\n      balanced={b['balanced']}  alt_line={b['alt_value']}"
                f"  mult={b['alt_mult']:.3f}  gap={b['gap']}"
                f"\n      -> BET HIGHER {b['alt_value']} — {b['gap']} easier than balanced at {b['alt_mult']:.3f}x payout!"
            )
    else:
        print("\n  No exploitable alternate bugs found.")

    if expiring:
        print(f"\n  EXPIRING LINES ({len(expiring)}) — visible within next {_EXPIRING_HOURS}h:")
        for b in expiring:
            exp = b.get("expires_at", "")[:19]
            print(
                f"    ! [{b['sport']}] {b['player']} {b['stat']}: "
                f"balanced={b['balanced']}  expires={exp}"
            )

    print(f"\n  Log: {UD_BUGS_LOG}")
    print(f"{'='*65}\n")


def _print_all_lines(grouped: dict):
    print(f"\n  All grouped lines (sport | player | stat | balanced | alternates):")
    for (name, stat, sport), entry in sorted(grouped.items(), key=lambda x: (x[0][2], x[0][0], x[0][1])):
        b = entry.get("balanced", "—")
        alts = ", ".join(f"{v}@{m:.3f}" for v, m in entry.get("alternates", []))
        exp = f"  [exp: {entry['expires_at'][:19]}]" if entry.get("expires_at") else ""
        print(f"    [{sport:<8}] {name:<28} {stat:<20} bal={b:<6}  alts=[{alts}]{exp}")


# ── Logging ────────────────────────────────────────────────────────────────────

def _log_bugs(bugs: list[dict]):
    if not bugs:
        return
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    write_header = not UD_BUGS_LOG.exists()
    ts = datetime.now(timezone.utc).isoformat()
    with open(UD_BUGS_LOG, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=UD_CSV_FIELDS)
        if write_header:
            writer.writeheader()
        for b in bugs:
            writer.writerow({
                "timestamp":      ts,
                "sport":          b["sport"],
                "player":         b["player"],
                "stat":           b["stat"],
                "balanced_line":  b["balanced"],
                "alt_line":       b["alt_value"],
                "alt_multiplier": b["alt_mult"],
                "gap":            b["gap"],
                "bug_type":       b["bug_type"],
            })


# ── Deduplication helpers ──────────────────────────────────────────────────────

def _load_seen() -> set:
    if SEEN_UD_BUGS_PATH.exists():
        try:
            return set(json.loads(SEEN_UD_BUGS_PATH.read_text()))
        except Exception:
            pass
    return set()


def _save_seen(seen: set):
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    SEEN_UD_BUGS_PATH.write_text(json.dumps(sorted(seen)))


def _bug_key(b: dict) -> str:
    return f"{b['player']}|{b['stat']}|{b['sport']}|{b['alt_value']}|{b['bug_type']}"


# ── Public scan functions ──────────────────────────────────────────────────────

def scan_underdog(sport_filter: str | None = None, show_all: bool = False) -> list[dict]:
    """
    Fetch lines, detect bugs, print results, log to CSV.

    Parameters
    ----------
    sport_filter : str, optional
        Case-insensitive sport_id filter (e.g. "NBA").
    show_all : bool
        If True, also print all grouped lines.

    Returns
    -------
    list of bug dicts
    """
    label = sport_filter.upper() if sport_filter else "ALL SPORTS"
    print(f"[ud] Fetching Underdog lines ({label})...")

    try:
        grouped, raw_lines = get_grouped_lines(sport_filter)
    except Exception as e:
        print(f"[ERROR] Underdog fetch failed: {e}")
        return []

    print(f"[ud] {len(raw_lines)} raw lines, {len(grouped)} (player, stat, sport) groups")

    bugs = _detect_bugs(grouped)
    _print_bugs(bugs, label)

    if show_all:
        _print_all_lines(grouped)

    _log_bugs(bugs)
    return bugs


def auto_scan_underdog(sport_filter: str | None = None) -> list[dict]:
    """
    Like scan_underdog but deduplicates against seen cache and sends alerts.
    New bugs are alerted via notify.py; already-seen bugs are skipped.
    """
    from notify import alert, send_email, format_ud_bugs_email

    label = sport_filter.upper() if sport_filter else "ALL SPORTS"
    print(f"[ud:auto] Fetching Underdog lines ({label})...")

    try:
        grouped, raw_lines = get_grouped_lines(sport_filter)
    except Exception as e:
        print(f"[ERROR] Underdog fetch failed: {e}")
        return []

    print(f"[ud:auto] {len(raw_lines)} raw lines, {len(grouped)} groups")

    bugs = _detect_bugs(grouped)
    _log_bugs(bugs)

    seen = _load_seen()
    new_bugs = [b for b in bugs if _bug_key(b) not in seen]

    if not new_bugs:
        print("[ud:auto] No new bugs — all previously seen.")
        return []

    print(f"[ud:auto] {len(new_bugs)} new bug(s) — sending alert...")
    _print_bugs(new_bugs, f"{label} (NEW)")

    # Build and send alert
    subject, html, plain = format_ud_bugs_email(new_bugs)
    sms_lines = []
    for b in new_bugs[:3]:  # cap SMS at 3 entries
        if b["bug_type"] == "easy_alternate":
            sms_lines.append(
                f"{b['player']} {b['stat']} [{b['sport']}]: "
                f"alt={b['alt_value']} vs bal={b['balanced']} "
                f"mult={b['alt_mult']:.3f} gap={b['gap']}"
            )
        else:
            sms_lines.append(
                f"{b['player']} {b['stat']} [{b['sport']}]: "
                f"EXPIRING bal={b['balanced']} exp={str(b.get('expires_at',''))[:16]}"
            )
    sms_msg = f"Underdog {len(new_bugs)} bug(s):\n" + "\n".join(sms_lines)

    alert(subject, html, plain, sms_msg)

    # Save new keys as seen
    for b in new_bugs:
        seen.add(_bug_key(b))
    _save_seen(seen)

    return new_bugs


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Underdog Fantasy bug scanner")
    parser.add_argument("--sport", default=None,
                        help="Filter by sport_id, case-insensitive (e.g. NBA, NFL)")
    parser.add_argument("--all-lines", action="store_true",
                        help="Print every grouped line, not just bugs")
    parser.add_argument("--auto", action="store_true",
                        help="Deduplicate against seen cache and send alerts via notify.py")
    args = parser.parse_args()

    if args.auto:
        auto_scan_underdog(sport_filter=args.sport)
    else:
        scan_underdog(sport_filter=args.sport, show_all=args.all_lines)
