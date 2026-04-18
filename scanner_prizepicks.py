"""
PrizePicks edge scanner.

Compares PrizePicks standard lines vs vig-adjusted sportsbook consensus,
enriched with NBA recent form (L5/L10 averages) and ESPN injury reports.

Run manually: python3 scanner_prizepicks.py

Output:
  - Prints top picks sorted by edge with stats context
  - Flags injured players and form mismatches
  - Logs all matched markets to logs/prizepicks_history.csv
  - Logs edge picks (>= threshold) to logs/prizepicks_edges.csv
"""

import csv
import re
import time
from datetime import datetime, UTC
from pathlib import Path

from data import odds, prizepicks, injuries as inj_module, nba_stats, action_network as an

EDGE_THRESHOLD = 0.05      # 5% minimum — PP's vig is ~14% per leg in 2-picks
MIN_BOOKS = 3              # Ignore edges backed by fewer than this many books

HISTORY_PATH = Path("logs/prizepicks_history.csv")
EDGES_PATH = Path("logs/prizepicks_edges.csv")

HISTORY_FIELDS = [
    "timestamp", "player", "stat_type", "pp_line",
    "fair_prob", "edge_over", "edge_under", "best_side", "best_edge",
    "books_used", "game",
]
EDGES_FIELDS = HISTORY_FIELDS


# ── Sportsbook data ───────────────────────────────────────────────────────────

def _parse_bookmaker_data(odds_data: dict, game_label: str, book_data: dict):
    """Parse a bookmakers response (Odds API or Action Network format) into book_data."""
    for bookmaker in odds_data.get("bookmakers", []):
        for market in bookmaker.get("markets", []):
            stat = market["key"]
            pairs = {}
            for o in market["outcomes"]:
                key = (o["description"], o["point"])
                if key not in pairs:
                    pairs[key] = {}
                pairs[key][o["name"]] = o

            for (player_name, book_line), sides in pairs.items():
                if "Over" not in sides or "Under" not in sides:
                    continue
                fair_over, _ = odds.remove_vig(
                    sides["Over"]["price"], sides["Under"]["price"]
                )
                last_name = re.sub(
                    r"\b(JR|SR|II|III|IV)\b\.?$",
                    "",
                    player_name.split()[-1].upper()
                ).strip()
                key = (last_name, stat)
                if key not in book_data:
                    book_data[key] = []
                book_data[key].append((book_line, fair_over, player_name, game_label))


def _get_all_book_lines() -> dict:
    """
    Fetch sportsbook props for tonight's NBA games.
    Tries The Odds API first (cached), falls back to Action Network if credits exhausted.
    Returns dict: (last_name_upper, odds_stat) -> [(book_line, fair_over, full_name, game)]
    """
    book_data = {}

    # --- Try The Odds API first ---
    odds_api_ok = False
    try:
        events = odds.get_events("nba")
        odds_api_ok = True
    except Exception as e:
        print(f"[odds] Unavailable ({e}) — switching to Action Network")

    if odds_api_ok:
        for event in events:
            game_label = f"{event['away_team']} @ {event['home_team']}"
            try:
                odds_data = odds.get_player_props(
                    "nba", event["id"],
                    ["player_points", "player_threes", "player_assists",
                     "player_rebounds", "player_turnovers"],
                )
                _parse_bookmaker_data(odds_data, game_label, book_data)
                print(f"[odds] Fetched {game_label}")
            except Exception as e:
                if "401" in str(e) or "Unauthorized" in str(e):
                    print(f"[odds] Credits exhausted — switching to Action Network")
                    odds_api_ok = False
                    break
                print(f"[warn] {game_label}: {e}")
            time.sleep(0.5)

    # --- Fall back to Action Network ---
    if not odds_api_ok or not book_data:
        print("[an]  Fetching from Action Network...")
        try:
            an_events = an.get_events()
            for event in an_events:
                if event.get("status") == "complete":
                    continue
                game_label = f"{event['away_team']} @ {event['home_team']}"
                try:
                    props = an.get_player_props(event["id"])
                    _parse_bookmaker_data(props, game_label, book_data)
                    n_books = len(props.get("bookmakers", []))
                    print(f"[an]  Fetched {game_label} ({n_books} books)")
                except Exception as e:
                    print(f"[an]  warn {game_label}: {e}")
                time.sleep(0.3)
        except Exception as e:
            print(f"[ERROR] Action Network also failed: {e}")

    return book_data


def _find_fair_prob(player: str, odds_stat: str, pp_line: float, book_data: dict):
    """
    Match a PrizePicks line to sportsbook consensus.

    Integer PP lines (e.g. 2.0):
      - Match sportsbook line 0.5 BELOW (diff = -0.5)
      - OVER 2.0 wins on 3+, pushes on 2 → matches book "over 1.5"
      - UNDER 2.0 wins on 0/1, pushes on 2 → uses (1 - fair_over) from same book line

    Half PP lines (e.g. 2.5):
      - Match sportsbook exact line

    Returns (fair_over, books_used, game_label) or None.
    """
    last_name = re.sub(r"\b(JR|SR|II|III|IV)\b\.?$", "", player.split()[-1].upper()).strip()
    entries = book_data.get((last_name, odds_stat), [])

    matching = []
    game_label = ""
    is_half = (pp_line % 1 == 0.5)

    for book_line, fair_over, _, game in entries:
        diff = book_line - pp_line
        if is_half:
            if abs(diff) > 0.05:
                continue
        else:
            if not (-0.6 < diff < 0.0):
                continue
        matching.append(fair_over)
        game_label = game

    if not matching:
        return None

    return sum(matching) / len(matching), len(matching), game_label


# ── Stats context ─────────────────────────────────────────────────────────────

def _form_label(side: str, pp_line: float, stats: dict | None) -> str:
    """
    Produce a context block showing averages, minutes, and per-36 rates.
    Flags when elevated/reduced playing time explains recent stat changes.
    """
    if not stats:
        return "        (no stats available)"

    s_avg  = stats["season_avg"]
    l10    = stats["l10_avg"]
    l5     = stats["l5_avg"]
    trend  = "↑" if l5 > l10 else ("↓" if l5 < l10 else "→")

    # Does recent form support the bet?
    form_vs_line = (l5 >= pp_line) if side == "OVER" else (l5 <= pp_line)
    support = "✓ form supports" if form_vs_line else "✗ form cuts against"

    rest_note = ""
    if stats.get("rest_games_removed", 0) > 0:
        rest_note = f"  ({stats['rest_games_removed']} rest game(s) excluded)"

    line1 = (
        f"        stats:  season={s_avg:.1f}  L10={l10:.1f}  "
        f"L5={l5:.1f}{trend}  last5={stats['last_5']}  [{support}]{rest_note}"
    )

    # Minutes context
    s_min  = stats["season_min"]
    l5_min = stats["l5_min"]
    mflag  = stats.get("minutes_flag")
    pct    = stats.get("min_change_pct", 0)
    p36_s  = stats["season_per36"]
    p36_l5 = stats["l5_per36"]
    p36_ch = stats["per36_change"]

    if mflag == "elevated":
        # More minutes recently — stat bump may be role-driven, not skill
        # Check if per-36 rate also went up (genuine improvement) or stayed flat (just minutes)
        if p36_ch > 0.5:
            mins_note = (
                f"⬆ minutes +{pct:.0%} (season {s_min:.0f}min → L5 {l5_min:.0f}min) "
                f"but per-36 also up {p36_s:.1f}→{p36_l5:.1f} — genuine improvement"
            )
        else:
            mins_note = (
                f"⚠ minutes +{pct:.0%} (season {s_min:.0f}min → L5 {l5_min:.0f}min) "
                f"— stat bump likely role-driven, per-36 flat ({p36_s:.1f}→{p36_l5:.1f})"
            )
    elif mflag == "reduced":
        mins_note = (
            f"⬇ minutes -{abs(pct):.0%} (season {s_min:.0f}min → L5 {l5_min:.0f}min) "
            f"— reduced role, per-36 {p36_s:.1f}→{p36_l5:.1f}"
        )
    else:
        mins_note = f"        minutes:  season={s_min:.0f}  L5={l5_min:.0f}  per-36 {p36_s:.1f}→{p36_l5:.1f}"

    if mflag:
        line2 = f"        minutes:  {mins_note}"
    else:
        line2 = mins_note

    return f"{line1}\n{line2}"


def _confidence(side: str, pp_line: float, books_used: int, stats: dict | None) -> str:
    """
    Return HIGH / MED / LOW based on books + form + minutes context.

    Key logic:
    - If minutes are elevated but per-36 is flat, the stat bump is role-driven
      and may not persist → we use per-36 projected value instead of raw L5
    - If minutes are reduced, the raw L5 understates true ability → more favorable
    """
    if books_used < MIN_BOOKS:
        return "LOW (few books)"

    if not stats:
        return "MED (no form data)"

    mflag  = stats.get("minutes_flag")
    p36_l5 = stats["l5_per36"]
    p36_s  = stats["season_per36"]
    s_min  = stats["season_min"]

    # Adjusted L5: if playing time is elevated, project the stat back at normal minutes
    # If playing time is reduced, project at normal minutes (shows true ability)
    if mflag and s_min > 0:
        projected_l5 = (p36_l5 / 36) * s_min
    else:
        projected_l5 = stats["l5_avg"]

    # Does adjusted recent form support the bet direction?
    if side == "OVER":
        form_ok = projected_l5 >= pp_line * 0.85
    else:
        form_ok = projected_l5 <= pp_line * 1.15

    # Minutes warning: elevated minutes that explain the edge weaken confidence
    minutes_boost = (mflag == "elevated" and stats.get("per36_change", 0) <= 0.5)

    if books_used >= 6 and form_ok and not minutes_boost:
        return "HIGH"
    elif books_used >= 5 and form_ok and not minutes_boost:
        return "HIGH"
    elif minutes_boost:
        return "MED (minutes-driven, may not persist)"
    elif books_used >= 4 and form_ok:
        return "MED"
    elif not form_ok:
        return "LOW (form cuts against)"
    else:
        return "MED"


# ── Logging ───────────────────────────────────────────────────────────────────

def _log_rows(path: Path, fields: list, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists()
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


# ── Main scanner ──────────────────────────────────────────────────────────────

def scan_prizepicks() -> list[dict]:
    print(f"\n{'='*60}")
    print(f"PRIZEPICKS SCAN: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"{'='*60}")

    # 1. PrizePicks lines
    try:
        projections = prizepicks.get_nba_projections()
        print(f"[pp]  {len(projections)} standard NBA projections tonight")
    except Exception as e:
        print(f"[ERROR] PrizePicks: {e}")
        return []

    if not projections:
        print("[pp] No projections available.")
        return []

    # 2. Injury report
    print("[inj] Fetching ESPN injury report...")
    injury_report = inj_module.get_injury_report()
    print(f"[inj] {len(injury_report)} players flagged")

    # 3. Sportsbook lines
    print("[odds] Fetching sportsbook consensus...")
    book_data = _get_all_book_lines()
    if not book_data:
        print("[ERROR] No sportsbook data.")
        return []

    # 4. Compare and find edges
    ts = datetime.now(UTC).isoformat()
    all_rows = []
    edge_rows = []
    no_match = 0
    skipped_injury = 0

    for proj in projections:
        # Check injury first — skip DNPs immediately
        inj = inj_module.check_player(proj["player"], injury_report)
        if inj and inj["disqualified"]:
            skipped_injury += 1
            continue

        result = _find_fair_prob(proj["player"], proj["odds_stat"], proj["line"], book_data)
        if not result:
            no_match += 1
            continue

        fair_prob, books_used, game = result
        edge_over = fair_prob - 0.5
        edge_under = 0.5 - fair_prob
        best_side = "OVER" if edge_over >= edge_under else "UNDER"
        best_edge = max(edge_over, edge_under)

        row = {
            "timestamp": ts,
            "player": proj["player"],
            "stat_type": proj["stat_type"],
            "pp_line": proj["line"],
            "fair_prob": round(fair_prob, 4),
            "edge_over": round(edge_over, 4),
            "edge_under": round(edge_under, 4),
            "best_side": best_side,
            "best_edge": round(best_edge, 4),
            "books_used": books_used,
            "game": game,
        }
        all_rows.append(row)

        if best_edge >= EDGE_THRESHOLD:
            edge_rows.append(row)

    # 5. Log raw data
    if all_rows:
        _log_rows(HISTORY_PATH, HISTORY_FIELDS, all_rows)
    if edge_rows:
        _log_rows(EDGES_PATH, EDGES_FIELDS, edge_rows)

    # 6. Fetch stats only for edge picks (keep it fast)
    stats_cache = {}
    if edge_rows:
        print(f"\n[stats] Fetching NBA game logs for {len(edge_rows)} edge pick(s)...")
        for e in edge_rows:
            key = (e["player"], e["stat_type"])
            if key not in stats_cache:
                stats_cache[key] = nba_stats.get_player_stats(e["player"], e["stat_type"])
                time.sleep(0.4)

    # 7. Dashboard
    print(f"\n{'='*60}")
    print(f"  Projections tonight  : {len(projections)}")
    print(f"  Matched to books     : {len(all_rows)}")
    print(f"  Skipped (injured)    : {skipped_injury}")
    print(f"  No sportsbook match  : {no_match}")
    print(f"  Edges ≥ {EDGE_THRESHOLD:.0%}           : {len(edge_rows)}")

    if edge_rows:
        top = sorted(edge_rows, key=lambda x: x["best_edge"], reverse=True)

        # Group by confidence
        high, med, low = [], [], []
        for e in top:
            s = stats_cache.get((e["player"], e["stat_type"]))
            conf = _confidence(e["best_side"], e["pp_line"], e["books_used"], s)
            e["_conf"] = conf
            e["_stats"] = s
            inj = inj_module.check_player(e["player"], injury_report)
            e["_inj"] = inj
            if conf.startswith("HIGH"):
                high.append(e)
            elif conf.startswith("MED"):
                med.append(e)
            else:
                low.append(e)

        def _print_pick(e, rank=None):
            prefix = f"  {rank}." if rank else "   "
            inj = e.get("_inj")
            inj_tag = f" ⚠️ {inj['status']} ({inj['detail']})" if inj else ""
            print(
                f"{prefix} {e['best_side']:5} {e['player']:<22} {e['pp_line']:5.1f} "
                f"{e['stat_type']:<12}  edge={e['best_edge']:+.1%}  "
                f"fair={e['fair_prob']:.1%}  ({e['books_used']} books){inj_tag}"
            )
            print(_form_label(e["best_side"], e["pp_line"], e["_stats"]))

        if high:
            print(f"\n  ── 🟢 HIGH CONFIDENCE ──")
            for i, e in enumerate(high, 1):
                _print_pick(e, i)

        if med:
            print(f"\n  ── 🟡 MEDIUM CONFIDENCE ──")
            for i, e in enumerate(med, 1):
                _print_pick(e, i)

        if low:
            print(f"\n  ── 🔴 LOW CONFIDENCE (use caution) ──")
            for i, e in enumerate(low, 1):
                _print_pick(e, i)

        # Parlay suggestions — HIGH confidence only
        parlay_picks = [e for e in high if e["best_edge"] >= 0.07]
        print(f"\n  ── 🎯 PARLAY IDEAS ──")
        if len(parlay_picks) >= 2:
            print(f"  2-pick Power (pays 3x — need ~58% each):")
            for p in parlay_picks[:4]:
                print(f"    ✓ {p['best_side']} {p['player']} {p['pp_line']} {p['stat_type']}")
        elif parlay_picks:
            print(f"  Only 1 HIGH-confidence pick ≥7% today. Wait for more or go solo.")
        else:
            print(f"  No HIGH-confidence picks above 7% tonight.")

    print(f"\n  History : {HISTORY_PATH}")
    print(f"  Edges   : {EDGES_PATH}")
    print(f"{'='*60}\n")

    return edge_rows


if __name__ == "__main__":
    scan_prizepicks()
