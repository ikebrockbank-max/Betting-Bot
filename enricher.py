"""
enricher.py — Signal reconciler and player context enricher.

Collects all new finds from a scan run, groups them by (player, stat),
fetches NBA stats + injury data, and produces a unified verdict for each
player so the notification email gives one clear recommendation even when
multiple platforms fire on the same pick.

Verdict types
─────────────
  OVER / UNDER      — all signals agree on direction
  LEAN_OVER/UNDER   — signals conflict but logic + stats resolve it
  CONFLICTING       — signals conflict and can't be resolved → skip
  AVOID             — player is listed Out / Doubtful

Signal weight system
──────────────────────────────────────────────────────────────────────────
Weights control priority when signals disagree.  The most important rule
is NOT a simple weight comparison — it's the DEMON-LINE vs CONSENSUS-LINE
relationship:

  1. DEMON BUG + CONSENSUS CONFLICT (most common conflict case)
     ─────────────────────────────────────────────────────────
     A PP demon bug means: you get HIGHER payout on an EASIER line.
     A consensus-under signal means: books think the player scores LESS
     than PrizePicks' standard line.

     The key question: is the demon line below or above what books expect?

     demon_line < consensus (books' expectation)
       → Books think the player WILL score above the demon line.
         You're getting demon payout on a >50% probability outcome.
         → STRONG OVER (demon line).  Best of both worlds.

     demon_line ≈ consensus  (within 2 units)
       → Uncertain territory. Demon payout helps but probability unclear.
         → LEAN OVER (demon line) with caution.

     demon_line > consensus (books' expectation)
       → Books think the player WON'T score above the demon line.
         Demon payout doesn't compensate for low probability.
         → LEAN UNDER.  Trust the consensus.

  2. ALL OTHER CONFLICTS — flat weight comparison
     ─────────────────────────────────────────────
     consensus   3   multiple sportsbooks = hardest to be wrong
     cross_plat  2   cross-platform line gap
     bug         1   PP demon/goblin mechanical bug
     flash       1   PP flash sale
     promo       1   PP/ParlayPlay promo line
     mono_bug    1   ParlayPlay multiplier reversal
     mispriced_alt 1 Underdog mispriced alternate

     Stats (L5 avg) add +2 weight to whichever side they support.
"""

from __future__ import annotations
import unicodedata
from collections import defaultdict

# ── Signal type weights ───────────────────────────────────────────────────────
_WEIGHT: dict[str, int] = {
    "consensus":     3,
    "cross_plat":    2,
    "bug":           1,
    "flash":         1,
    "promo":         1,
    "mono_bug":      1,
    "mispriced_alt": 1,
}

# NBA league IDs / sport strings where stats enrichment applies
_NBA_LEAGUES = {"nba", "7", "237", "3", "252", "wnba"}


def _norm_name(name: str) -> str:
    nfkd = unicodedata.normalize("NFKD", name)
    return nfkd.encode("ascii", "ignore").decode("ascii").lower().strip()


# ── Signal normalizers ────────────────────────────────────────────────────────
# Each returns a standardized dict:
#   src, sig_type, direction ("over"|"under"), bet_line, display, raw

def _game_time_from(raw: dict) -> str:
    """Extract a human-readable game time from any raw signal dict."""
    st = raw.get("start_time") or raw.get("game_time") or ""
    if not st:
        return ""
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(st.replace("Z", "+00:00"))
        # Convert to Eastern Time (UTC-4 in EDT, UTC-5 in EST — approximate)
        from datetime import timedelta
        et = dt - timedelta(hours=4)
        return et.strftime("%a %b %-d, %-I:%M %p ET")
    except Exception:
        return st[:16].replace("T", " ")


def _from_pp_bug(b: dict) -> dict | None:
    bug_type = b.get("bug_type", "")
    if bug_type in ("demon_easy", "demon_eq_standard", "line_moved_demon_easy"):
        gap = b.get("gap", 0)
        return {
            "src": "pp", "sig_type": "bug", "direction": "over",
            "bet_line": b["bug_line"],
            "game_time": _game_time_from(b),
            "display": (
                f"PP demon {b['bug_line']} vs std {b['standard']}"
                + (f" (+{gap} easier)" if gap else " (same line, demon payout)")
            ),
            "raw": b,
        }
    return None


def _from_flash(s: dict) -> dict | None:
    return {
        "src": "pp", "sig_type": "flash", "direction": "over",
        "bet_line": s["sale_line"],
        "game_time": _game_time_from(s),
        "display": f"PP flash sale {s['normal_line']} → {s['sale_line']} (−{s['discount']})",
        "raw": s,
    }


def _from_consensus(e: dict) -> dict | None:
    return {
        "src": "pp", "sig_type": "consensus", "direction": e["direction"],
        "bet_line": e["platform_line"],
        "consensus_line": e.get("consensus"),   # sportsbook market line
        "game_time": _game_time_from(e.get("raw", e)),
        "display": (
            f"Sportsbooks ({e.get('source','').replace('_',' ')}) say {e['consensus']} "
            f"vs PP {e['platform_line']} ({e['diff']:+.1f}, {e['pct_diff']}% off) "
            f"→ bet {e['direction'].upper()}"
        ),
        "raw": e,
    }


def _from_plp(b: dict) -> dict | None:
    bug_type = b.get("bug_type", "")
    if "cross" in bug_type:
        if b.get("direction") == "parlayplay_lower":
            return {
                "src": "parlayplay", "sig_type": "cross_plat", "direction": "over",
                "bet_line": b["parlayplay_line"],
                "display": f"ParlayPlay {b['parlayplay_line']} < PP {b['pp_line']} — bet MORE on ParlayPlay",
                "raw": b,
            }
        else:
            return {
                "src": "parlayplay", "sig_type": "cross_plat", "direction": "under",
                "bet_line": b["parlayplay_line"],
                "display": f"ParlayPlay {b['parlayplay_line']} > PP {b['pp_line']} — bet LESS on ParlayPlay",
                "raw": b,
            }
    elif "reversal" in bug_type:
        if "more_mult" in bug_type:
            return {
                "src": "parlayplay", "sig_type": "mono_bug", "direction": "over",
                "bet_line": b.get("line_high"),
                "display": (
                    f"ParlayPlay More-mult reversal: {b['line_high']} pays {b['mult_high']}x "
                    f"< lower {b['line_low']} ({b['mult_low']}x)"
                ),
                "raw": b,
            }
        else:
            return {
                "src": "parlayplay", "sig_type": "mono_bug", "direction": "under",
                "bet_line": b.get("line_low"),
                "display": (
                    f"ParlayPlay Less-mult reversal: {b['line_low']} pays {b['mult_low']}x "
                    f"> higher {b['line_high']} ({b['mult_high']}x)"
                ),
                "raw": b,
            }
    elif "promo" in bug_type:
        if "more" in bug_type:
            return {
                "src": "parlayplay", "sig_type": "promo", "direction": "over",
                "bet_line": b.get("promo_line"),
                "display": (
                    f"ParlayPlay 🔥 MORE {b['promo_line']} ({b['promo_mult']}x) "
                    f"> hard line {b['hard_line']} ({b['hard_mult']}x)"
                ),
                "raw": b,
            }
        else:
            return {
                "src": "parlayplay", "sig_type": "promo", "direction": "under",
                "bet_line": b.get("promo_line"),
                "display": (
                    f"ParlayPlay 🔥 LESS {b['promo_line']} ({b['promo_mult']}x) "
                    f"> easy line {b.get('easy_line')} ({b.get('easy_mult')}x)"
                ),
                "raw": b,
            }
    return None


def _from_ud(b: dict) -> dict | None:
    if b.get("bug_type") == "mispriced_alternate":
        return {
            "src": "underdog", "sig_type": "mispriced_alt", "direction": "over",
            "bet_line": b["alt_value"],
            "display": (
                f"Underdog alt {b['alt_value']} (bal {b['balanced']}) "
                f"at {b['alt_mult']:.3f}x — easier at same or better payout"
            ),
            "raw": b,
        }
    return None


# ── Conflict resolver ─────────────────────────────────────────────────────────

def _resolve(signals: list[dict], stats: dict | None, injury: dict | None) -> dict:
    """Turn normalized signals + context into a single verdict dict."""

    # Injury first
    if injury and injury.get("disqualified"):
        return {
            "verdict": "AVOID", "confidence": "SKIP", "bet_line": None,
            "reason": f"Player listed {injury['status'].upper()}: {injury['headline']}",
        }

    overs  = [s for s in signals if s["direction"] == "over"]
    unders = [s for s in signals if s["direction"] == "under"]

    l5_avg  = stats["l5_avg"]  if stats else None
    l10_avg = stats["l10_avg"] if stats else None

    def stat_vs(line: float) -> str | None:
        """Does L5 avg support over or under this line?"""
        if l5_avg is None or line is None:
            return None
        ratio = l5_avg / line
        if ratio > 1.08:
            return "over"
        if ratio < 0.92:
            return "under"
        return "neutral"

    def _reason_stats(direction: str, line: float) -> str:
        if l5_avg is None:
            return "No recent NBA stats available."
        diff = round(l5_avg - line, 1)
        sign = "+" if diff >= 0 else ""
        trend = "↑" if direction == "over" else "↓"
        return (
            f"L5 avg {l5_avg} ({sign}{diff} vs line {line}) "
            f"{trend} {'confirms' if (direction=='over' and diff>0) or (direction=='under' and diff<0) else 'cautions against'} this bet."
        )

    # ── All agree ─────────────────────────────────────────────────────────────
    if overs and not unders:
        bet_line   = min((s["bet_line"] for s in overs if s["bet_line"] is not None), default=None)
        tot_weight = sum(_WEIGHT.get(s["sig_type"], 1) for s in overs)
        sd         = stat_vs(bet_line) if bet_line else None
        conf = "STRONG" if (tot_weight >= 3 or sd == "over") else "LEAN"
        return {
            "verdict": "OVER", "confidence": conf, "bet_line": bet_line,
            "reason": _reason_stats("over", bet_line) if bet_line else "Multiple signals agree: bet OVER.",
        }

    if unders and not overs:
        bet_line   = max((s["bet_line"] for s in unders if s["bet_line"] is not None), default=None)
        tot_weight = sum(_WEIGHT.get(s["sig_type"], 1) for s in unders)
        sd         = stat_vs(bet_line) if bet_line else None
        conf = "STRONG" if (tot_weight >= 3 or sd == "under") else "LEAN"
        return {
            "verdict": "UNDER", "confidence": conf, "bet_line": bet_line,
            "reason": _reason_stats("under", bet_line) if bet_line else "Multiple signals agree: bet UNDER.",
        }

    # ── Conflict ──────────────────────────────────────────────────────────────
    if overs and unders:
        over_w  = sum(_WEIGHT.get(s["sig_type"], 1) for s in overs)
        under_w = sum(_WEIGHT.get(s["sig_type"], 1) for s in unders)

        min_over  = min((s["bet_line"] for s in overs  if s["bet_line"] is not None), default=None)
        max_under = max((s["bet_line"] for s in unders if s["bet_line"] is not None), default=None)

        over_desc  = " | ".join(s["display"] for s in overs)
        under_desc = " | ".join(s["display"] for s in unders)
        conflict_detail = f"↑ OVER: {over_desc}\n↓ UNDER: {under_desc}"

        # ── Special case: demon bug (OVER) vs consensus (UNDER) ──────────────
        # Compare the demon line directly against what sportsbooks expect.
        # This is more accurate than flat weight comparison.
        demon_overs      = [s for s in overs  if s["sig_type"] == "bug"]
        consensus_unders = [s for s in unders if s["sig_type"] == "consensus"]

        if demon_overs and consensus_unders:
            demon_line     = min(s["bet_line"] for s in demon_overs if s["bet_line"] is not None)
            consensus_line = min(
                (s["consensus_line"] for s in consensus_unders if s.get("consensus_line") is not None),
                default=None,
            )

            if consensus_line is not None:
                gap_to_consensus = demon_line - consensus_line   # + = demon above books

                l5_note = ""
                if l5_avg is not None:
                    if l5_avg > demon_line:
                        l5_note = f" L5 avg {l5_avg} also confirms over ({demon_line}) ✓"
                    elif l5_avg < demon_line:
                        l5_note = f" L5 avg {l5_avg} is below demon line ({demon_line}) — caution."

                if gap_to_consensus <= 0:
                    # BEST CASE: demon line ≤ books' expected outcome
                    # → High probability + demon payout = strong edge
                    conf = "STRONG" if (l5_avg is None or l5_avg >= demon_line) else "LEAN"
                    return {
                        "verdict": "OVER", "confidence": conf,
                        "bet_line": demon_line,
                        "reason": (
                            f"Demon line {demon_line} is BELOW sportsbook consensus ({consensus_line}) — "
                            f"books expect the player to score MORE than this. "
                            f"You get demon-payout on a high-probability outcome.{l5_note}"
                        ),
                        "conflict_detail": conflict_detail,
                    }
                elif gap_to_consensus <= 2.0:
                    # Borderline — demon slightly above consensus, payout still helps
                    return {
                        "verdict": "LEAN_OVER", "confidence": "CAUTION",
                        "bet_line": demon_line,
                        "reason": (
                            f"Demon line {demon_line} is {gap_to_consensus:.1f} above books ({consensus_line}) — "
                            f"close enough that demon payout gives slight EV edge, but uncertain.{l5_note}"
                        ),
                        "conflict_detail": conflict_detail,
                    }
                else:
                    # Demon line well above consensus — probability too low, trust books
                    return {
                        "verdict": "LEAN_UNDER", "confidence": "CAUTION",
                        "bet_line": max_under,
                        "reason": (
                            f"Demon line {demon_line} is {gap_to_consensus:.1f} ABOVE sportsbook consensus "
                            f"({consensus_line}). Even the easier line is harder than books think — "
                            f"trust the consensus.{l5_note}"
                        ),
                        "conflict_detail": conflict_detail,
                    }

        # ── General conflict: flat weights + stats tiebreaker ─────────────────
        if l5_avg is not None and min_over is not None:
            if l5_avg > min_over * 1.05:
                over_w += 2
            elif l5_avg < min_over * 0.95:
                under_w += 2

        edge = over_w - under_w

        if edge >= 2:
            bet_line  = min_over
            stat_note = f" L5 avg {l5_avg} {'supports' if (l5_avg or 0) > (bet_line or 0) else 'is neutral on'} the over." if l5_avg else ""
            return {
                "verdict": "LEAN_OVER", "confidence": "CAUTION", "bet_line": bet_line,
                "reason": f"Signals conflict, but OVER outweighs UNDER ({over_w} vs {under_w}).{stat_note}",
                "conflict_detail": conflict_detail,
            }
        elif edge <= -2:
            bet_line  = max_under
            stat_note = f" L5 avg {l5_avg} {'supports' if (l5_avg or 0) < (bet_line or 0) else 'is neutral on'} the under." if l5_avg else ""
            return {
                "verdict": "LEAN_UNDER", "confidence": "CAUTION", "bet_line": bet_line,
                "reason": f"Signals conflict, but UNDER outweighs OVER ({under_w} vs {over_w}).{stat_note}",
                "conflict_detail": conflict_detail,
            }
        else:
            stat_note = f" Stats (L5 {l5_avg}) do not break the tie." if l5_avg else ""
            return {
                "verdict": "CONFLICTING", "confidence": "SKIP", "bet_line": None,
                "reason": f"Signals directly contradict each other — skip this pick.{stat_note}",
                "conflict_detail": conflict_detail,
            }

    return {"verdict": "UNKNOWN", "confidence": "SKIP", "bet_line": None, "reason": "No signals."}


# ── Sort key ──────────────────────────────────────────────────────────────────
_SORT_ORDER = {
    ("STRONG", "OVER"):        0,
    ("STRONG", "UNDER"):       0,
    ("LEAN",   "OVER"):        1,
    ("LEAN",   "UNDER"):       1,
    ("CAUTION","LEAN_OVER"):   2,
    ("CAUTION","LEAN_UNDER"):  2,
    ("SKIP",   "CONFLICTING"): 3,
    ("SKIP",   "AVOID"):       4,
}


# ── Main entry point ──────────────────────────────────────────────────────────

def build_verdicts(
    new_bugs:      list[dict],
    new_flash:     list[dict],
    new_promos:    list[dict],
    new_consensus: list[dict],
    new_plp:       list[dict],
    new_ud:        list[dict],
    fetch_stats:   bool = True,
) -> list[dict]:
    """
    Group all new finds by (player, stat), enrich with stats + injury,
    and return a sorted list of verdict dicts.

    Each verdict dict:
        player, stat, league,
        verdict  (OVER|UNDER|LEAN_OVER|LEAN_UNDER|CONFLICTING|AVOID),
        confidence (STRONG|LEAN|CAUTION|SKIP),
        bet_line, reason, conflict_detail?,
        signals  [list of normalized signal dicts],
        stats    {season_avg, l10_avg, l5_avg, last_5, l5_min, minutes_flag, ...} | None,
        injury   {status, headline, ...} | None,
    """
    groups:    dict[tuple[str,str], list[dict]] = defaultdict(list)
    league_map: dict[tuple[str,str], str]        = {}

    def _add(key, sig, league=""):
        if sig:
            groups[key].append(sig)
            league_map.setdefault(key, league)

    for b in new_bugs:
        _add((b["player"], b["stat"]), _from_pp_bug(b), b.get("league", ""))

    for s in new_flash:
        _add((s["player"], s["stat"]), _from_flash(s), s.get("league", ""))

    for p in new_promos:
        key = (p["player"], p["stat"])
        # Promo demon lines are directional OVER picks
        if p.get("odds_type") == "demon":
            _add(key, {
                "src": "pp", "sig_type": "promo", "direction": "over",
                "bet_line": p["line"],
                "display": f"PP promo demon {p['line']} — boosted payout",
                "raw": p,
            }, p.get("league", ""))
        else:
            league_map.setdefault(key, p.get("league", ""))

    for e in new_consensus:
        _add((e["player"], e["stat"]), _from_consensus(e), e.get("league", ""))

    for b in new_plp:
        _add((b["player"], b["stat"]), _from_plp(b), "NBA")

    for b in new_ud:
        _add((b["player"], b["stat"]), _from_ud(b), b.get("sport", ""))

    if not groups:
        return []

    # ── Fetch injuries + stats ────────────────────────────────────────────────
    stats_map:  dict[tuple[str,str], dict] = {}
    injury_map: dict[str, dict]             = {}

    if fetch_stats:
        try:
            from data.injuries import get_injury_report, check_player
            inj_report = get_injury_report()
            for (player, _) in groups:
                inj = check_player(player, inj_report)
                if inj:
                    injury_map[player] = inj
        except Exception as e:
            print(f"[enricher] Injury fetch failed (non-fatal): {e}")

        nba_pairs = [
            (player, stat)
            for (player, stat) in groups
            if league_map.get((player, stat), "").lower() in _NBA_LEAGUES | {""}
        ]
        if nba_pairs:
            try:
                from data.nba_stats import get_stats_bulk
                stats_map = get_stats_bulk(nba_pairs, delay=0.35)
                hit  = len(stats_map)
                miss = len(nba_pairs) - hit
                print(f"[enricher] Stats fetched for {hit}/{len(nba_pairs)} player-stat pairs"
                      + (f" ({miss} no data)" if miss else ""))
                if miss:
                    missing = [(p, s) for (p, s) in nba_pairs if (p, s) not in stats_map]
                    for p, s in missing[:5]:
                        print(f"[enricher]   no stats: {p} / {s}")
            except Exception as e:
                print(f"[enricher] Stats fetch failed (non-fatal): {e}")

    # ── Build verdicts ────────────────────────────────────────────────────────
    verdicts: list[dict] = []
    for (player, stat), signals in groups.items():
        stats  = stats_map.get((player, stat))
        injury = injury_map.get(player)
        league = league_map.get((player, stat), "")

        resolved = _resolve(signals, stats, injury)

        # Pick the earliest non-empty game time across all signals
        game_time = next(
            (s.get("game_time") for s in signals if s.get("game_time")), ""
        )

        verdicts.append({
            "player":          player,
            "stat":            stat,
            "league":          league,
            "game_time":       game_time,
            "verdict":         resolved["verdict"],
            "confidence":      resolved["confidence"],
            "bet_line":        resolved.get("bet_line"),
            "reason":          resolved.get("reason", ""),
            "conflict_detail": resolved.get("conflict_detail"),
            "signals":         signals,
            "stats":           stats,
            "injury":          injury if injury and (injury.get("warning") or injury.get("disqualified")) else None,
        })

    verdicts.sort(key=lambda v: _SORT_ORDER.get((v["confidence"], v["verdict"]), 5))
    return verdicts
