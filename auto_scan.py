"""
auto_scan.py — scheduled bug scanner with email + SMS alerts.

Runs the full all-leagues PrizePicks bug scan, deduplicates against
previously-seen bugs, and fires email/SMS alerts for anything new.

Also sends macOS notifications when running locally.

Run manually:   python3 auto_scan.py
Cloud loop:     python3 scheduler.py   (runs this every SCAN_INTERVAL_MIN minutes)
"""

import json
import os
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
    find_multiplier_value_bugs,
    HOURS_AHEAD_DEFAULT,
)
from scanner_consensus import find_consensus_edges, find_correlated_legs, print_consensus_edges
from notify import (
    send_push,
    send_email,
    format_digest_email,
)

SEEN_BUGS_PATH      = Path("logs/.seen_bugs.json")
SEEN_FLASH_PATH     = Path("logs/.seen_flash.json")
SEEN_PROMO_PATH     = Path("logs/.seen_promos.json")
SEEN_CONSENSUS_PATH = Path("logs/.seen_consensus.json")
SEEN_PLP_PATH       = Path("logs/.seen_parlayplay.json")
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

def _plp_bug_key(b: dict) -> str:
    bug_type = b.get("bug_type", "")
    player   = b.get("player", "")
    stat     = b.get("stat", "")
    if "cross" in bug_type:
        return f"cross|{player}|{stat}|{b.get('direction','')}"
    elif "mono" in bug_type or "reversal" in bug_type:
        return f"mono|{player}|{stat}|{b.get('line_low','')}|{b.get('line_high','')}"
    else:
        return f"promo|{player}|{stat}|{b.get('promo_line','')}"


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
    mult_bugs      = find_multiplier_value_bugs(groups)
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

    # Multiplier value bugs: goblin-no-adjustment = exploit (alert!), demon-no-adjustment = trap (log only)
    mult_exploits = [b for b in mult_bugs if b["bug_type"] == "goblin_no_adjustment"]
    mult_traps    = [b for b in mult_bugs if b["bug_type"] == "demon_no_adjustment"]
    if mult_traps:
        for b in mult_traps:
            _log(f"  ⛔ {b['league']} | {b['player']} {b['stat']} demon={b['bug_line']} — hard pick at standard payout, AVOID")
    if mult_exploits:
        _log(f"🔥 {len(mult_exploits)} MULTIPLIER VALUE BUG(S) — goblin paying standard rate!")
        for b in mult_exploits:
            _log(f"  🔥 {b['league']} | {b['player']} {b['stat']} goblin={b['bug_line']} std={b['standard']} — easy pick at standard payout!")
        top = mult_exploits[0]
        push_msg = (f"MULT BUG: {top['player']} {top['stat']} goblin={top['bug_line']} "
                    f"paying standard rate — easy pick!")
        if len(mult_exploits) > 1:
            push_msg += f" +{len(mult_exploits)-1} more"
        send_push(push_msg, title="Multiplier Value Bug!")

    # ── ParlayPlay scan ───────────────────────────────────────────────────────
    plp_mono_bugs   = []
    plp_promo_bugs  = []
    plp_cross_edges = []
    try:
        from scanner_parlayplay import (
            find_monotonicity_bugs,
            find_promo_value_bugs,
            find_cross_platform_edges,
        )
        from data.parlayplay import get_grouped_lines as get_plp_lines
        _log("🎰 Running ParlayPlay scan...")
        plp_grouped, _ = get_plp_lines()
        plp_mono_bugs   = find_monotonicity_bugs(plp_grouped)
        plp_promo_bugs  = find_promo_value_bugs(plp_grouped)
        plp_cross_edges = find_cross_platform_edges(groups, plp_grouped)
        _log(f"  ParlayPlay: {len(plp_mono_bugs)} mono bugs, "
             f"{len(plp_promo_bugs)} promo bugs, "
             f"{len(plp_cross_edges)} cross-platform edges")
    except Exception as e:
        _log(f"  ParlayPlay scan error (non-fatal): {e}")

    all_plp_finds = plp_mono_bugs + plp_promo_bugs + plp_cross_edges
    seen_plp      = _load_seen(SEEN_PLP_PATH)
    new_plp       = [b for b in all_plp_finds if _plp_bug_key(b) not in seen_plp]
    for b in new_plp:
        seen_plp.add(_plp_bug_key(b))
    _save_seen(SEEN_PLP_PATH, seen_plp)

    if new_plp:
        _log(f"🎰 {len(new_plp)} NEW PARLAYPLAY FIND(S):")
        for b in new_plp[:8]:
            btype = b.get("bug_type", "")
            if "cross" in btype:
                _log(f"  📊 {b['player']} {b['stat']}: PP={b['pp_line']} vs PLP={b['parlayplay_line']} "
                     f"(gap={b['gap']:+.1f}, {b['pct_gap']}%) → {b['action']}")
            elif "reversal" in btype:
                _log(f"  ⚡ {b['player']} {b['stat']}: {b['direction']} mult reversal "
                     f"at {b['line_low']}→{b['line_high']} ({b['mult_low']}→{b['mult_high']})")
            else:
                _log(f"  🔥 {b['player']} {b['stat']}: {b['action']}")

    # ── Underdog scan — disabled (UNDERDOG_SCAN=true to re-enable) ───────────
    new_ud = []
    if os.getenv("UNDERDOG_SCAN", "").lower() == "true":
        try:
            from scanner_underdog import _detect_bugs as _ud_detect
            from scanner_underdog import _load_seen as _ud_load_seen
            from scanner_underdog import _save_seen as _ud_save_seen
            from scanner_underdog import _bug_key   as _ud_bug_key
            from data.underdog import get_grouped_lines as _get_ud_lines
            _log("🐶 Running Underdog scan...")
            ud_grouped, _ = _get_ud_lines()
            ud_bugs = _ud_detect(ud_grouped)
            seen_ud = _ud_load_seen()
            new_ud  = [b for b in ud_bugs if _ud_bug_key(b) not in seen_ud]
            for b in new_ud:
                seen_ud.add(_ud_bug_key(b))
            _ud_save_seen(seen_ud)
            _log(f"  Underdog: {len(new_ud)} new bug(s) of {len(ud_bugs)} total")
        except Exception as e:
            _log(f"  Underdog scan error (non-fatal): {e}")

    total_new = len(new_bugs) + len(new_flash) + len(new_promos) + len(new_consensus) + len(new_plp) + len(new_ud)

    if total_new == 0:
        _log(f"Scan complete — {len(all_bugs)} PP bugs, {len(flash_sales)} flash, "
             f"{len(promos)} promos, {len(consensus_edges)} consensus edges, "
             f"{len(all_plp_finds)} PLP finds — nothing new.")
        return

    # ── Log all new finds ─────────────────────────────────────────────────────
    if new_bugs:
        _log(f"🚨 {len(new_bugs)} NEW PP BUG(S):")
        for b in new_bugs:
            gap_str = f"gap={b['gap']}" if b.get("gap", 0) > 0 else "SAME LINE"
            moved   = f" [std moved {b['prev_std']}→{b['standard']}]" if b.get("prev_std") else ""
            _log(f"  ★ {b['league']} | {b['player']} {b['stat']} | "
                 f"demon={b['bug_line']} std={b['standard']} ({gap_str}){moved} | "
                 f"{b.get('start_time','')[:16]}")

    if new_flash:
        _log(f"⚡ {len(new_flash)} NEW FLASH SALE(S):")
        for s in new_flash:
            _log(f"  ⚡ {s['league']} | {s['player']} {s['stat']} | "
                 f"{s['normal_line']}→{s['sale_line']} (−{s['discount']})")

    if new_promos:
        _log(f"🎯 {len(new_promos)} NEW PROMO(S):")
        for p in new_promos:
            _log(f"  🎯 {p['league']} | {p['player']} {p['stat']} | {p['odds_type']} {p['line']}")

    if new_consensus:
        _log(f"📊 {len(new_consensus)} NEW CONSENSUS EDGE(S):")
        for e in new_consensus:
            _log(f"  📊 {e['league']} | {e['player']} {e['stat']} | "
                 f"PP={e['platform_line']} books={e['consensus']} ({e['diff']:+.1f}) → BET {e['direction'].upper()}")
        if new_correlated:
            _log(f"  ★ {len(new_correlated)} correlated parlay(s) found")
        print_consensus_edges(new_consensus, new_correlated, "PP")

    if new_ud:
        _log(f"🐶 {len(new_ud)} NEW UNDERDOG BUG(S):")
        for b in new_ud:
            _log(f"  🐶 [{b['sport']}] {b['player']} {b['stat']}: alt={b['alt_value']} bal={b['balanced']} mult={b.get('alt_mult',0):.3f}")

    # ── Quick push notifications (one per platform type) ──────────────────────
    if new_bugs:
        top = new_bugs[0]
        sms = f"PP demon: {top['player']} {top['stat']} {top['bug_line']} vs std {top['standard']}"
        if len(new_bugs) > 1:
            sms += f" +{len(new_bugs)-1}"
        send_push(sms, title="PP Bug Found!")
        _mac_notify("PP Bug!", sms)

    if new_flash:
        top = new_flash[0]
        sms = f"Flash sale: {top['player']} {top['stat']} {top['normal_line']}→{top['sale_line']} ACT FAST"
        send_push(sms, title="PP Flash Sale!")
        _mac_notify("Flash Sale!", sms)

    if new_consensus:
        top = new_consensus[0]
        sms = (f"Line edge: {top['player']} {top['stat']} PP={top['platform_line']} "
               f"books={top['consensus']} BET {top['direction'].upper()}")
        if len(new_consensus) > 1:
            sms += f" +{len(new_consensus)-1}"
        send_push(sms, title="Consensus Edge!")
        _mac_notify("Consensus Edge!", sms)

    if new_plp:
        top = new_plp[0]
        sms = f"ParlayPlay: {top['player']} {top['stat']} — {top.get('action','')[:100]}"
        if len(new_plp) > 1:
            sms += f" +{len(new_plp)-1}"
        send_push(sms, title="ParlayPlay Bug!")
        _mac_notify("ParlayPlay Bug!", sms)

    if new_ud:
        top = new_ud[0]
        sms = f"Underdog: {top['player']} {top['stat']} alt={top['alt_value']} bal={top['balanced']} ({top.get('alt_mult',0):.3f}x)"
        if len(new_ud) > 1:
            sms += f" +{len(new_ud)-1}"
        send_push(sms, title="Underdog Bug!")
        _mac_notify("Underdog Bug!", sms)

    # ── Build enriched digest email (single email, all platforms, with stats) ─
    try:
        from enricher import build_verdicts
        _log("📊 Building enriched digest (fetching player stats + injury report)...")
        verdicts = build_verdicts(
            new_bugs=new_bugs,
            new_flash=new_flash,
            new_promos=new_promos,
            new_consensus=new_consensus,
            new_plp=new_plp,
            new_ud=new_ud,
        )
        if verdicts:
            subject, html, plain = format_digest_email(verdicts)
            send_email(subject, html, plain)
            # Log verdicts
            for v in verdicts:
                conf  = v["confidence"]
                verd  = v["verdict"]
                bline = f" @ {v['bet_line']}" if v.get("bet_line") else ""
                _log(f"  📋 [{conf}] {v['player']} {v['stat']}: {verd}{bline} — {v['reason'][:80]}")
        _log(f"  Digest: {len(verdicts)} verdict(s) emailed")
    except Exception as e:
        _log(f"  Digest email error (non-fatal): {e}")


if __name__ == "__main__":
    run()
