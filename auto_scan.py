"""
auto_scan.py — scheduled bug scanner with email + SMS alerts.

Runs the full all-leagues PrizePicks bug scan, deduplicates against
previously-seen bugs, and fires email/SMS alerts for anything new.

Also sends macOS notifications when running locally.

Run manually:   python3 auto_scan.py
Cloud loop:     python3 scheduler.py   (runs this every SCAN_INTERVAL_MIN minutes)
"""

import json
import subprocess
import sys
from datetime import datetime, UTC
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from scanner_bugs import (
    _fetch_all_projections,
    _group_lines,
    _find_bugs,
    find_line_movement_bugs,
    find_flash_sales,
    find_promos,
    find_adjusted_standard_lines,
    HOURS_AHEAD_DEFAULT,
)
from scanner_consensus import find_consensus_edges, find_correlated_legs, print_consensus_edges
from notify import (
    alert,
    format_bugs_email,
    format_flash_email,
    format_promo_email,
    format_consensus_email,
    send_sms,
)

SEEN_BUGS_PATH      = Path("logs/.seen_bugs.json")
SEEN_FLASH_PATH     = Path("logs/.seen_flash.json")
SEEN_PROMO_PATH     = Path("logs/.seen_promos.json")
SEEN_CONSENSUS_PATH = Path("logs/.seen_consensus.json")
SCAN_LOG_PATH       = Path("logs/auto_scan.log")
HOURS_AHEAD         = 168  # 7 days ahead


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_seen(path: Path) -> set:
    if path.exists():
        try:
            return set(json.loads(path.read_text()))
        except Exception:
            pass
    return set()


def _save_seen(path: Path, seen: set):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(seen)))


def _bug_key(b: dict) -> str:
    return f"{b['player']}|{b['stat']}|{b['game_id']}|{b['bug_line']}|{b['bug_type']}"

def _flash_key(s: dict) -> str:
    return f"{s['player']}|{s['stat']}|{s['game_id']}|{s['sale_line']}"

def _promo_key(p: dict) -> str:
    return f"{p['player']}|{p['stat']}|{p['game_id']}|{p['odds_type']}|{p['line']}"

def _consensus_key(e: dict) -> str:
    return f"{e['player']}|{e['stat']}|{e['direction']}|{e['platform_line']}|{e['consensus']}"


def _mac_notify(title: str, message: str):
    """macOS Notification Center — silently skipped on non-Mac / cloud."""
    try:
        script = f'display notification "{message}" with title "{title}" sound name "Ping"'
        subprocess.run(["osascript", "-e", script], check=True, capture_output=True)
    except Exception:
        pass


def _log(msg: str):
    ts   = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    line = f"[{ts}] {msg}"
    print(line)
    SCAN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SCAN_LOG_PATH, "a") as f:
        f.write(line + "\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    _log("=== Auto-scan started ===")

    try:
        projections, players = _fetch_all_projections(league_id=None, hours_ahead=HOURS_AHEAD)
        _log(f"Fetched {len(projections)} projections, {len(players)} players")
    except Exception as e:
        _log(f"ERROR fetching projections: {e}")
        return

    groups, _ = _group_lines(projections, players, HOURS_AHEAD)

    static_bugs    = _find_bugs(groups)
    move_bugs      = find_line_movement_bugs(groups)
    flash_sales    = find_flash_sales(projections, players, HOURS_AHEAD)
    promos         = find_promos(projections, players, HOURS_AHEAD)
    adj_standards  = find_adjusted_standard_lines(groups)
    consensus_edges = find_consensus_edges(projections, players)
    correlated     = find_correlated_legs(consensus_edges)

    actionable = [b for b in static_bugs if b["bug_type"] in ("demon_easy", "demon_eq_standard")]
    # Only alert on demon-type movement bugs — goblin_hard are traps to avoid, not bet signals
    move_demon_bugs  = [b for b in move_bugs if b["bug_type"] == "line_moved_demon_easy"]
    move_goblin_traps = [b for b in move_bugs if b["bug_type"] == "line_moved_goblin_hard"]
    all_bugs   = actionable + move_demon_bugs

    seen_bugs      = _load_seen(SEEN_BUGS_PATH)
    seen_flash     = _load_seen(SEEN_FLASH_PATH)
    seen_promo     = _load_seen(SEEN_PROMO_PATH)
    seen_consensus = _load_seen(SEEN_CONSENSUS_PATH)

    new_bugs      = [b for b in all_bugs        if _bug_key(b)       not in seen_bugs]
    new_flash     = [s for s in flash_sales      if _flash_key(s)     not in seen_flash]
    new_promos    = [p for p in promos           if _promo_key(p)     not in seen_promo]
    new_consensus = [e for e in consensus_edges  if _consensus_key(e) not in seen_consensus]

    for b in new_bugs:      seen_bugs.add(_bug_key(b))
    for s in new_flash:     seen_flash.add(_flash_key(s))
    for p in new_promos:    seen_promo.add(_promo_key(p))
    for e in new_consensus: seen_consensus.add(_consensus_key(e))

    _save_seen(SEEN_BUGS_PATH,      seen_bugs)
    _save_seen(SEEN_FLASH_PATH,     seen_flash)
    _save_seen(SEEN_PROMO_PATH,     seen_promo)
    _save_seen(SEEN_CONSENSUS_PATH, seen_consensus)

    # Correlated parlays for new consensus edges only
    new_correlated = find_correlated_legs(new_consensus)

    # Log adjusted-odds standard lines — direction unknown, user must verify in app
    boosted = [a for a in adj_standards if a["likely_boosted"]]
    if boosted:
        _log(f"💰 {len(boosted)} standard line(s) with likely-boosted multiplier (no demon above):")
        for a in boosted[:5]:
            _log(f"  💰 {a['league']} | {a['player']} {a['stat']} | "
                 f"standard={a['line']} goblins={a['goblin_ladder']} | VERIFY MULTIPLIER IN APP")

    # Log goblin traps for awareness (no alert — these are "avoid" warnings, not bet signals)
    if move_goblin_traps:
        _log(f"⚠️  {len(move_goblin_traps)} goblin trap(s) detected (std dropped below goblin — avoid):")
        for b in move_goblin_traps:
            _log(f"  ⚠ {b['league']} | {b['player']} {b['stat']} | "
                 f"goblin={b['bug_line']} > std={b['standard']} [std moved {b.get('prev_std','')}→{b['standard']}]")

    total_new = len(new_bugs) + len(new_flash) + len(new_promos) + len(new_consensus)

    if total_new == 0:
        _log(f"Scan complete — {len(all_bugs)} bugs, {len(flash_sales)} flash, "
             f"{len(promos)} promos, {len(consensus_edges)} consensus edges — nothing new.")
        return

    # ── New bugs ──────────────────────────────────────────────────────────────
    if new_bugs:
        _log(f"🚨 {len(new_bugs)} NEW BUG(S):")
        for b in new_bugs:
            gap_str    = f"gap={b['gap']}" if b.get("gap", 0) > 0 else "SAME LINE"
            moved      = f" [std moved {b['prev_std']}→{b['standard']}]" if b.get("prev_std") else ""
            line_label = "goblin" if "goblin" in b.get("bug_type", "") else "demon"
            _log(f"  ★ {b['league']} | {b['player']} {b['stat']} | "
                 f"{line_label}={b['bug_line']} std={b['standard']} ({gap_str}){moved} | "
                 f"{b.get('start_time','')[:16]}")

        subject, html, plain = format_bugs_email(new_bugs)
        sms = f"🚨 PP Bug: {new_bugs[0]['player']} {new_bugs[0]['stat']} demon={new_bugs[0]['bug_line']} vs std={new_bugs[0]['standard']}"
        if len(new_bugs) > 1:
            sms += f" (+{len(new_bugs)-1} more)"
        alert(subject, html, plain, sms)
        _mac_notify(subject, sms)

    # ── Flash sales ───────────────────────────────────────────────────────────
    if new_flash:
        _log(f"⚡ {len(new_flash)} NEW FLASH SALE(S):")
        for s in new_flash:
            _log(f"  ⚡ {s['league']} | {s['player']} {s['stat']} | "
                 f"{s['normal_line']}→{s['sale_line']} (−{s['discount']}) | "
                 f"{s.get('start_time','')[:16]}")

        subject, html, plain = format_flash_email(new_flash)
        sms = f"⚡ PP Flash Sale: {new_flash[0]['player']} {new_flash[0]['stat']} {new_flash[0]['normal_line']}→{new_flash[0]['sale_line']} ACT FAST"
        alert(subject, html, plain, sms)
        _mac_notify(subject, sms)

    # ── Promos ────────────────────────────────────────────────────────────────
    if new_promos:
        _log(f"🎯 {len(new_promos)} NEW PROMO(S):")
        for p in new_promos:
            _log(f"  🎯 {p['league']} | {p['player']} {p['stat']} | "
                 f"{p['odds_type']} {p['line']} (PROMO) | {p.get('start_time','')[:16]}")

        subject, html, plain = format_promo_email(new_promos)
        names = ", ".join(f"{p['player']} {p['stat']}" for p in new_promos[:2])
        sms   = f"🎯 PP Promo: {names}" + (f" +{len(new_promos)-2} more" if len(new_promos) > 2 else "")
        alert(subject, html, plain, sms)
        _mac_notify(subject, sms)

    # ── Consensus edges ───────────────────────────────────────────────────────
    if new_consensus:
        _log(f"📊 {len(new_consensus)} NEW CONSENSUS EDGE(S):")
        for e in new_consensus:
            confirmed_tag = " ★MULT" if e.get("multiplier_confirmed") else ""
        _log(f"  📊{confirmed_tag} {e['league']} | {e['player']} {e['stat']} | "
                 f"PP={e['platform_line']} consensus={e['consensus']} "
                 f"({e['diff']:+.1f}, {e['pct_diff']}% off) → BET {e['direction'].upper()}")

        if new_correlated:
            _log(f"  ★ {len(new_correlated)} correlated parlay(s) found")

        subject, html, plain = format_consensus_email(new_consensus, new_correlated)
        top = new_consensus[0]
        sms = (f"PP Line Edge: {top['player']} {top['stat']} "
               f"line={top['platform_line']} vs consensus={top['consensus']} "
               f"({top['pct_diff']}% off) BET {top['direction'].upper()}")
        if len(new_consensus) > 1:
            sms += f" +{len(new_consensus)-1} more"
        alert(subject, html, plain, sms)
        print_consensus_edges(new_consensus, new_correlated, "PP")
        _mac_notify(subject, sms)


if __name__ == "__main__":
    run()
