"""
pp_playoff_report.py — Pre-game PrizePicks parlay report.

Fetches current PP lines, injury reports, and playoff/recent stats for every
active player in tonight's NBA games. Scores each pick algorithmically, builds
optimal 2-pick and 3-pick parlay combos, and emails a detailed breakdown.

Triggered by scheduler.py (PLAYOFF_REPORT_INTERVAL_MIN=30).
The report fires once per game when tip-off is 2.5–3.5 hours away.

Also runnable directly:
  python3 pp_playoff_report.py           # respects 3-hour window
  python3 pp_playoff_report.py --force   # runs for ALL tonight's games now
"""

import itertools
import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))

from data.nba_stats import get_player_stats
from data.injuries import get_injury_report
from notify import send_push, send_email

# ── Constants ──────────────────────────────────────────────────────────────────

SENT_LOG_PATH = Path("logs/.sent_pp_reports.json")
LOG_PATH      = Path("logs/pp_playoff_reports.log")

PP_API_URL    = "https://partner-api.prizepicks.com/projections"
PP_HEADERS    = {
    "User-Agent": "PrizePicks/2.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)",
    "Accept":     "application/json",
}

ESPN_URL     = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
ESPN_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

# Stat types to pull from PP and score
ANALYZE_STATS = {
    "Points", "Rebounds", "Assists", "3-PT Made", "Turnovers",
    "Blocks", "Steals", "Pts+Reb+Ast", "Pts+Reb", "Pts+Ast",
}

# PrizePicks payouts and break-even rates
PP_PAYOUTS   = {2: 3.0, 3: 5.0, 4: 10.0}
PP_BREAKEVEN = {2: 0.577, 3: 0.585, 4: 0.562}

# Minimum per-pick confidence to include in parlay pool
MIN_PICK_PROB = 0.55

# Minimum combined parlay probability to recommend
MIN_PARLAY_PROB = 0.28

# Window for triggering the report (minutes before tip-off)
REPORT_WINDOW_MIN = (140, 220)   # ~2h20m–3h40m


# ── Logging ────────────────────────────────────────────────────────────────────

def _log(msg: str):
    ts   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


# ── Data Fetchers ──────────────────────────────────────────────────────────────

def get_todays_games() -> list[dict]:
    """
    Fetch today's NBA games from ESPN scoreboard.
    Returns upcoming games sorted by start time.
    """
    try:
        resp = requests.get(ESPN_URL, headers=ESPN_HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        _log(f"[espn] Schedule fetch failed: {e}")
        return []

    games = []
    now   = datetime.now(timezone.utc)

    for event in data.get("events", []):
        comps       = event.get("competitions", [{}])[0]
        competitors = comps.get("competitors", [])

        teams = {}
        for c in competitors:
            side = c.get("homeAway", "home")
            t    = c.get("team", {})
            teams[side] = {
                "name":  t.get("shortDisplayName", ""),
                "abbr":  t.get("abbreviation", ""),
                "full":  t.get("displayName", ""),
            }

        home = teams.get("home", {})
        away = teams.get("away", {})

        try:
            start = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
        except Exception:
            continue

        mins_until = (start - now).total_seconds() / 60

        # Skip games that already started (more than 5 min ago) or >24h away
        if mins_until < -5 or mins_until > 24 * 60:
            continue

        games.append({
            "game_id":       event.get("id", ""),
            "name":          f"{away.get('abbr','')} @ {home.get('abbr','')}",
            "home_team":     home.get("name", ""),
            "away_team":     away.get("name", ""),
            "home_full":     home.get("full", ""),
            "away_full":     away.get("full", ""),
            "home_abbr":     home.get("abbr", ""),
            "away_abbr":     away.get("abbr", ""),
            "start_time":    start,
            "mins_until":    mins_until,
        })

    games.sort(key=lambda g: g["start_time"])
    return games


def fetch_pp_projections() -> list[dict]:
    """
    Fetch all standard, pre-game, single-stat NBA projections from PrizePicks.
    Returns only stats in ANALYZE_STATS. Broader than data/prizepicks.py —
    no Odds API mapping filter.
    """
    params = {"league_id": 7, "per_page": 500, "single_stat": "true"}
    for attempt in range(4):
        try:
            resp = requests.get(PP_API_URL, headers=PP_HEADERS, params=params, timeout=15)
            if resp.status_code == 429:
                wait = 20 * (attempt + 1)
                _log(f"[pp] Rate limited — waiting {wait}s (attempt {attempt+1}/4)")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            break
        except requests.exceptions.HTTPError as e:
            if attempt < 3:
                time.sleep(15)
                continue
            _log(f"[pp] Fetch failed after retries: {e}")
            return []
        except Exception as e:
            _log(f"[pp] Fetch failed: {e}")
            return []
    else:
        _log("[pp] All retry attempts exhausted")
        return []

    # Build player name lookup
    players = {}
    for item in data.get("included", []):
        if item.get("type") == "new_player":
            attrs = item.get("attributes", {})
            players[item["id"]] = {
                "name": attrs.get("name", ""),
                "team": attrs.get("team", ""),
            }

    projections = []
    now = datetime.now(timezone.utc)

    for proj in data.get("data", []):
        if proj.get("type") != "projection":
            continue
        attrs = proj.get("attributes", {})

        if attrs.get("projection_type", "Single Stat") != "Single Stat":
            continue
        if attrs.get("status") != "pre_game":
            continue
        if attrs.get("odds_type") != "standard":
            continue

        stat_type = attrs.get("stat_type", "")
        if stat_type not in ANALYZE_STATS:
            continue

        # Only tonight's games (within 12 hours)
        start_str = attrs.get("start_time", "")
        try:
            start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            if start > now + timedelta(hours=12) or start < now - timedelta(hours=1):
                continue
        except (ValueError, TypeError):
            continue

        try:
            line = float(attrs["line_score"])
        except (ValueError, KeyError):
            continue

        player_id   = proj.get("relationships", {}).get("new_player", {}).get("data", {}).get("id", "")
        player_info = players.get(player_id, {})
        player_name = player_info.get("name", "")

        if not player_name or "+" in player_name:
            continue

        projections.append({
            "player":     player_name,
            "team":       player_info.get("team", ""),
            "stat_type":  stat_type,
            "line":       line,
            "game_id":    attrs.get("game_id", ""),
            "start_time": start,
        })

    _log(f"[pp] {len(projections)} projections fetched across {len(ANALYZE_STATS)} stat types")
    return projections


# ── Pick Scoring ───────────────────────────────────────────────────────────────

def _score_pick(
    player: str,
    stat_type: str,
    line: float,
    direction: str,
    stats: dict,
    injury: dict | None,
) -> tuple[float, str]:
    """
    Score a single pick direction. Returns (probability 0-1, human reason string).
    Uses L5 hit rate, L5 avg gap, season avg, minutes trend, and injury.
    """
    l5 = stats.get("last_5", [])
    n5 = len(l5)
    if n5 == 0:
        return 0.5, "Insufficient recent data"

    # ── 1. L5 hit rate (50% weight) ──────────────────────────────────────────
    if direction == "OVER":
        hits = sum(1 for v in l5 if v > line)
    else:
        hits = sum(1 for v in l5 if v < line)
    l5_rate = hits / n5

    # ── 2. Avg gap from line (30% weight) ────────────────────────────────────
    l5_avg  = stats.get("l5_avg",  line)
    l10_avg = stats.get("l10_avg", line)
    s_avg   = stats.get("season_avg", line)

    gap     = l5_avg - line          # positive = above line
    gap_pct = gap / line if line > 0 else 0

    if direction == "OVER":
        buffer_score = min(0.85, max(0.20, 0.50 + gap_pct * 2.5))
    else:
        buffer_score = min(0.85, max(0.20, 0.50 - gap_pct * 2.5))

    # ── 3. Season avg alignment (10% weight) ─────────────────────────────────
    s_gap     = s_avg - line
    s_gap_pct = s_gap / line if line > 0 else 0
    if direction == "OVER":
        s_score = min(0.80, max(0.25, 0.50 + s_gap_pct * 1.5))
    else:
        s_score = min(0.80, max(0.25, 0.50 - s_gap_pct * 1.5))

    # ── 4. Trend bonus (per-36 rate change) ──────────────────────────────────
    trend        = stats.get("per36_change", 0)
    trend_bonus  = 0.0
    trend_note   = ""
    if direction == "OVER" and trend > 1.5:
        trend_bonus = 0.03
        trend_note  = "trending up per-36"
    elif direction == "UNDER" and trend < -1.5:
        trend_bonus = 0.03
        trend_note  = "trending down per-36"
    elif direction == "OVER" and trend < -2.5:
        trend_bonus = -0.03
    elif direction == "UNDER" and trend > 2.5:
        trend_bonus = -0.03

    # ── 5. Minutes flag ───────────────────────────────────────────────────────
    min_bonus   = 0.0
    min_note    = ""
    l5_min      = stats.get("l5_min", 0)
    s_min       = stats.get("season_min", 0)
    mflag       = stats.get("minutes_flag")
    if mflag == "elevated":
        if direction == "OVER":
            min_bonus = 0.025
            min_note  = f"minutes up ({l5_min:.0f}m L5 vs {s_min:.0f}m season)"
        else:
            min_bonus = -0.02
    elif mflag == "reduced":
        if direction == "UNDER":
            min_bonus = 0.025
            min_note  = f"minutes down ({l5_min:.0f}m L5 vs {s_min:.0f}m season)"
        else:
            min_bonus = -0.025
            min_note  = f"⚠ minutes reduced ({l5_min:.0f}m)"

    # ── 6. Injury penalty ─────────────────────────────────────────────────────
    inj_bonus = 0.0
    inj_note  = ""
    if injury:
        if injury.get("disqualified"):
            return 0.0, f"OUT — {injury.get('detail', 'injury')} (skip)"
        elif injury.get("warning"):
            inj_bonus = -0.06
            inj_note  = f"⚠ {injury.get('status','Questionable')} ({injury.get('detail','')})"

    # ── Combine ───────────────────────────────────────────────────────────────
    prob = (
        l5_rate      * 0.50
        + buffer_score * 0.30
        + s_score      * 0.10
        + 0.50         * 0.10   # neutral baseline
        + trend_bonus
        + min_bonus
        + inj_bonus
    )
    prob = max(0.10, min(0.94, prob))

    # ── Build reason string ───────────────────────────────────────────────────
    dir_word = "above" if direction == "OVER" else "below"
    gap_sign = f"+{gap:.1f}" if gap >= 0 else f"{gap:.1f}"
    hit_note = f"{hits}/{n5} last-5 games {dir_word} {line}"
    avg_note = f"L5 avg {l5_avg:.1f} ({gap_sign} vs line)"

    parts = [hit_note, avg_note]
    if s_avg != line:
        parts.append(f"season avg {s_avg:.1f}")
    if trend_note:
        parts.append(trend_note)
    if min_note:
        parts.append(min_note)
    if inj_note:
        parts.append(inj_note)

    return prob, " • ".join(parts)


def score_all_picks(
    projections: list[dict],
    injury_report: dict,
) -> list[dict]:
    """
    Score every PP projection. Returns list of pick dicts with prob and reason.
    Keeps the best direction (OVER or UNDER) per (player, stat_type).
    Skips picks for injured (OUT/Doubtful) players.
    """
    picks = []
    seen  = set()

    for proj in projections:
        player    = proj["player"]
        stat_type = proj["stat_type"]
        line      = proj["line"]
        key       = (player.lower(), stat_type)

        if key in seen:
            continue

        # Fetch stats (playoff first, then regular season fallback)
        try:
            stats = get_player_stats(player, stat_type)
        except Exception:
            stats = None

        if not stats:
            continue

        # Injury lookup (exact + last-name fallback)
        inj = injury_report.get(player.lower())
        if not inj:
            last = player.lower().split()[-1]
            inj  = next((v for k, v in injury_report.items() if k.endswith(last)), None)

        # Score both directions, keep the better one
        best = None
        for direction in ("OVER", "UNDER"):
            prob, reason = _score_pick(player, stat_type, line, direction, stats, inj)
            if prob >= MIN_PICK_PROB:
                if best is None or prob > best["prob"]:
                    best = {
                        "player":    player,
                        "stat_type": stat_type,
                        "line":      line,
                        "direction": direction,
                        "team":      proj.get("team", ""),
                        "game_id":   proj.get("game_id", ""),
                        "prob":      round(prob, 3),
                        "reason":    reason,
                        "stats":     stats,
                        "injury":    inj,
                    }

        if best:
            picks.append(best)
            seen.add(key)

        time.sleep(0.35)   # gentle on NBA stats API

    picks.sort(key=lambda p: p["prob"], reverse=True)
    return picks


# ── Parlay Builder ─────────────────────────────────────────────────────────────

def _corr_factor(picks: list[dict]) -> float:
    """
    Adjust combined probability for correlation between picks.
    Same-team OVERs are positively correlated (stack bonus).
    """
    teams = [p["team"] for p in picks]
    dirs  = [p["direction"] for p in picks]

    # Same-team OVER stack
    same_team_overs = sum(
        1 for i in range(len(picks))
        if teams.count(teams[i]) > 1 and dirs[i] == "OVER"
    )
    if same_team_overs >= 2:
        return 1.06

    # Same player, different stats — mild positive correlation
    players = [p["player"].lower() for p in picks]
    if len(players) != len(set(players)):
        return 1.02

    return 1.00


def build_parlays(picks: list[dict], sizes: list[int] = (2, 3)) -> list[dict]:
    """
    Generate all parlay combinations of `sizes` picks.
    Returns list sorted by EV (expected value), best first.
    """
    # One pick per (player, stat_type) — keep highest probability
    deduped: dict = {}
    for p in picks:
        k = (p["player"].lower(), p["stat_type"])
        if k not in deduped or p["prob"] > deduped[k]["prob"]:
            deduped[k] = p
    pool = sorted(deduped.values(), key=lambda x: x["prob"], reverse=True)

    parlays = []
    for size in sizes:
        for combo in itertools.combinations(pool, size):
            combo = list(combo)

            # No duplicate players in a combo
            names = [c["player"].lower() for c in combo]
            if len(names) != len(set(names)):
                continue

            base_prob    = 1.0
            for pick in combo:
                base_prob *= pick["prob"]

            combined = base_prob * _corr_factor(combo)
            if combined < MIN_PARLAY_PROB:
                continue

            payout = PP_PAYOUTS.get(size, size)
            ev     = combined * payout - (1 - combined)

            parlays.append({
                "picks":    combo,
                "size":     size,
                "combined": round(combined, 3),
                "payout":   payout,
                "ev":       round(ev, 3),
            })

    parlays.sort(key=lambda x: (x["ev"], x["combined"]), reverse=True)
    return parlays


# ── Email Templates ────────────────────────────────────────────────────────────

def _last5_boxes_html(vals: list, line: float, direction: str) -> str:
    BOX = (
        '<span style="display:inline-block;width:30px;height:30px;line-height:30px;'
        'text-align:center;border-radius:5px;font-size:12px;font-weight:800;'
        'margin-right:4px;background:{bg};color:#fff;">{v}</span>'
    )
    html = []
    for v in vals[:5]:
        hit = (v > line) if direction == "OVER" else (v < line)
        bg  = "#16a34a" if hit else "#dc2626"
        dv  = int(v) if v == int(v) else round(v, 1)
        html.append(BOX.format(bg=bg, v=dv))
    return "".join(html)


def _conf_bar_html(prob: float) -> str:
    """Colored progress bar for confidence level."""
    pct   = int(prob * 100)
    color = "#16a34a" if prob >= 0.68 else "#f59e0b" if prob >= 0.58 else "#ef4444"
    label = "HIGH" if prob >= 0.68 else "MEDIUM" if prob >= 0.58 else "LOW"
    filled = pct
    empty  = 100 - pct
    return f"""\
<table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:10px;">
  <tr>
    <td width="50" style="font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:0.4px;vertical-align:middle;">Confidence</td>
    <td style="padding:0 8px;vertical-align:middle;">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td width="{filled}%" style="background:{color};height:7px;border-radius:4px 0 0 4px;font-size:0;">&nbsp;</td>
          <td width="{empty}%" style="background:#e5e7eb;height:7px;border-radius:0 4px 4px 0;font-size:0;">&nbsp;</td>
        </tr>
      </table>
    </td>
    <td width="60" align="right" style="font-size:13px;font-weight:800;color:{color};white-space:nowrap;">{pct}% {label}</td>
  </tr>
</table>"""


def _pick_card_html(pick: dict) -> str:
    direction = pick["direction"]
    line      = pick["line"]
    player    = pick["player"]
    stat_type = pick["stat_type"]
    prob      = pick["prob"]
    reason    = pick["reason"]
    stats     = pick.get("stats", {})
    injury    = pick.get("injury")

    is_over   = direction == "OVER"
    accent    = "#16a34a" if is_over else "#dc2626"
    badge_bg  = "#dcfce7" if is_over else "#fee2e2"
    badge_fg  = "#14532d" if is_over else "#7f1d1d"
    badge_txt = f"OVER {line}" if is_over else f"UNDER {line}"

    l5_boxes = _last5_boxes_html(stats.get("last_5", []), line, direction)
    conf_bar = _conf_bar_html(prob)

    l5_avg  = stats.get("l5_avg",     "—")
    l10_avg = stats.get("l10_avg",    "—")
    s_avg   = stats.get("season_avg", "—")
    l5_min  = stats.get("l5_min",     "—")

    inj_banner = ""
    if injury:
        status    = injury.get("status", "Questionable")
        detail    = injury.get("detail", "")
        inj_color = "#dc2626" if injury.get("disqualified") else "#d97706"
        inj_bg    = "#fee2e2" if injury.get("disqualified") else "#fef3c7"
        inj_banner = (
            f'<tr><td style="background:{inj_bg};padding:7px 14px;border-top:1px solid #e5e7eb;">'
            f'<span style="color:{inj_color};font-size:12px;font-weight:700;">'
            f'&#9888;&#65039; {status}{" — " + detail if detail else ""}</span></td></tr>'
        )

    return f"""\
<table width="100%" cellpadding="0" cellspacing="0"
       style="margin-bottom:10px;border-radius:8px;overflow:hidden;border:1px solid #e5e7eb;">
  <tr>
    <td style="background:{accent};padding:10px 14px;">
      <table width="100%" cellpadding="0" cellspacing="0"><tr>
        <td>
          <p style="margin:0;color:#fff;font-size:15px;font-weight:800;">{player}</p>
          <p style="margin:1px 0 0;color:rgba(255,255,255,0.8);font-size:12px;">{stat_type}</p>
        </td>
        <td align="right">
          <span style="background:{badge_bg};color:{badge_fg};font-size:13px;font-weight:800;
                       padding:4px 12px;border-radius:20px;white-space:nowrap;">{badge_txt}</span>
        </td>
      </tr></table>
    </td>
  </tr>
  <tr>
    <td style="background:#fff;padding:12px 14px;">
      {conf_bar}
      <p style="margin:0 0 5px;font-size:11px;color:#6b7280;text-transform:uppercase;
                letter-spacing:0.4px;">Last 5 Games</p>
      <p style="margin:0 0 12px;">{l5_boxes if l5_boxes else '<em style="color:#9ca3af;font-size:12px;">No recent data</em>'}</p>
      <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:10px;">
        <tr>
          <td style="text-align:center;padding:3px 0;border-right:1px solid #f3f4f6;">
            <p style="margin:0;font-size:10px;color:#9ca3af;text-transform:uppercase;">L5 Avg</p>
            <p style="margin:2px 0 0;font-size:17px;font-weight:800;color:#111827;">{l5_avg}</p>
          </td>
          <td style="text-align:center;padding:3px 0;border-right:1px solid #f3f4f6;">
            <p style="margin:0;font-size:10px;color:#9ca3af;text-transform:uppercase;">L10 Avg</p>
            <p style="margin:2px 0 0;font-size:17px;font-weight:800;color:#111827;">{l10_avg}</p>
          </td>
          <td style="text-align:center;padding:3px 0;border-right:1px solid #f3f4f6;">
            <p style="margin:0;font-size:10px;color:#9ca3af;text-transform:uppercase;">Season</p>
            <p style="margin:2px 0 0;font-size:17px;font-weight:800;color:#111827;">{s_avg}</p>
          </td>
          <td style="text-align:center;padding:3px 0;">
            <p style="margin:0;font-size:10px;color:#9ca3af;text-transform:uppercase;">L5 Min</p>
            <p style="margin:2px 0 0;font-size:17px;font-weight:800;color:#111827;">{l5_min}</p>
          </td>
        </tr>
      </table>
      <p style="margin:0;font-size:12px;color:#4b5563;line-height:1.6;
                border-top:1px solid #f3f4f6;padding-top:8px;">{reason}</p>
    </td>
  </tr>
  {inj_banner}
</table>"""


def _parlay_card_html(parlay: dict, rank: int) -> str:
    size     = parlay["size"]
    combined = parlay["combined"]
    payout   = parlay["payout"]
    ev       = parlay["ev"]
    picks    = parlay["picks"]

    ev_color = "#16a34a" if ev > 0.60 else "#f59e0b" if ev > 0.20 else "#6b7280"
    ev_sign  = f"+{ev:.2f}" if ev >= 0 else f"{ev:.2f}"

    legs = " + ".join(
        f"{p['player'].split()[-1]} {p['direction']} {p['line']}"
        for p in picks
    )

    pick_cards_html = "".join(_pick_card_html(p) for p in picks)

    # Breakeven note
    be = PP_BREAKEVEN.get(size, 0.58)
    be_pct = int(be * 100)

    return f"""\
<table width="100%" cellpadding="0" cellspacing="0"
       style="margin-bottom:20px;border-radius:10px;overflow:hidden;border:2px solid #818cf8;">
  <tr>
    <td style="background:#1e1b4b;padding:14px 18px;">
      <table width="100%" cellpadding="0" cellspacing="0"><tr>
        <td>
          <p style="margin:0;color:#fff;font-size:17px;font-weight:800;">
            #{rank} &nbsp;&#8212;&nbsp; {size}-Pick Parlay</p>
          <p style="margin:3px 0 0;color:#a5b4fc;font-size:12px;">{legs}</p>
        </td>
        <td align="right" style="white-space:nowrap;padding-left:12px;">
          <p style="margin:0;color:#fbbf24;font-size:22px;font-weight:800;">{payout}x</p>
          <p style="margin:2px 0 0;color:#86efac;font-size:12px;">{int(combined*100)}% combined</p>
        </td>
      </tr></table>
    </td>
  </tr>
  <tr>
    <td style="background:#f8fafc;padding:4px 8px 8px;">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td style="padding:8px 6px 4px;">
            <table cellpadding="0" cellspacing="0">
              <tr>
                <td style="padding-right:16px;">
                  <p style="margin:0;font-size:10px;color:#6b7280;text-transform:uppercase;">EV per $1</p>
                  <p style="margin:2px 0 0;font-size:16px;font-weight:800;color:{ev_color};">{ev_sign}</p>
                </td>
                <td style="padding-right:16px;">
                  <p style="margin:0;font-size:10px;color:#6b7280;text-transform:uppercase;">Break-even</p>
                  <p style="margin:2px 0 0;font-size:16px;font-weight:800;color:#374151;">{be_pct}% each</p>
                </td>
                <td>
                  <p style="margin:0;font-size:10px;color:#6b7280;text-transform:uppercase;">Payout</p>
                  <p style="margin:2px 0 0;font-size:16px;font-weight:800;color:#374151;">${payout} per $1 risked</p>
                </td>
              </tr>
            </table>
          </td>
        </tr>
      </table>
    </td>
  </tr>
  <tr>
    <td style="background:#f8fafc;padding:0 14px 14px;">
      {pick_cards_html}
    </td>
  </tr>
</table>"""


def format_report_email(
    parlays:   list[dict],
    all_picks: list[dict],
    games:     list[dict],
) -> tuple[str, str, str]:
    """Build the full pre-game parlay report email. Returns (subject, html, plain)."""

    now      = datetime.now(timezone.utc)
    ts       = now.strftime("%b %d %Y %H:%M UTC")

    # Game header info
    if games:
        first_game  = games[0]
        start_time  = first_game["start_time"]
        eastern     = start_time - timedelta(hours=4)   # rough UTC→ET
        tip_str     = eastern.strftime("%I:%M %p ET").lstrip("0")
        mins_until  = first_game["mins_until"]
        mins_str    = f"{int(mins_until//60)}h {int(mins_until%60)}m to tip-off"

        if len(games) == 1:
            away_full   = first_game.get("away_full", first_game["away_team"])
            home_full   = first_game.get("home_full", first_game["home_team"])
            game_header = f"{away_full} @ {home_full}"
        else:
            game_header = f"{len(games)} Games Tonight"

        subject = f"🏀 PP Parlay Report: {game_header} — {tip_str}"
    else:
        tip_str     = "Tonight"
        mins_str    = ""
        game_header = "NBA Tonight"
        subject     = "🏀 PP Parlay Report — NBA Tonight"

    # ── Injury context ────────────────────────────────────────────────────────
    out_players  = [p for p in all_picks if p.get("injury") and p["injury"].get("disqualified")]
    q_players    = [p for p in all_picks if p.get("injury") and p["injury"].get("warning")]

    inj_rows = ""
    if out_players:
        names = ", ".join(sorted(set(p["player"] for p in out_players)))
        inj_rows += (
            f'<p style="margin:0 0 5px;font-size:13px;color:#dc2626;">'
            f'<strong>OUT:</strong> {names}</p>'
        )
    if q_players:
        names = ", ".join(sorted(set(p["player"] for p in q_players)))
        inj_rows += (
            f'<p style="margin:0 0 5px;font-size:13px;color:#d97706;">'
            f'<strong>Questionable:</strong> {names}</p>'
        )
    if not inj_rows:
        inj_rows = '<p style="margin:0;font-size:13px;color:#16a34a;">No significant injury concerns.</p>'

    # Games list (multi-game nights)
    game_rows = ""
    for g in games:
        et = (g["start_time"] - timedelta(hours=4)).strftime("%I:%M %p ET").lstrip("0")
        m  = int(g["mins_until"])
        game_rows += (
            f'<p style="margin:0 0 3px;font-size:13px;color:#374151;">'
            f'<strong>{g["away_full"]} @ {g["home_full"]}</strong> &nbsp;&#8226;&nbsp; {et} ({m}m)</p>'
        )

    context_card = f"""\
<table width="100%" cellpadding="0" cellspacing="0"
       style="margin-bottom:20px;border-radius:8px;overflow:hidden;border:1px solid #bae6fd;">
  <tr>
    <td style="background:#f0f9ff;padding:14px 16px;">
      <p style="margin:0 0 10px;font-size:14px;font-weight:700;color:#0369a1;">
        &#128203; Tonight's Games</p>
      {game_rows if game_rows else '<p style="margin:0;color:#6b7280;font-size:13px;">Schedule unavailable</p>'}
      <p style="margin:12px 0 6px;font-size:14px;font-weight:700;color:#0369a1;">
        &#128681; Injury Report</p>
      {inj_rows}
      <p style="margin:10px 0 0;font-size:11px;color:#64748b;">
        Generated {ts} &nbsp;&#8226;&nbsp; Always check PP app for last-minute line changes before placing</p>
    </td>
  </tr>
</table>"""

    # ── Top picks overview table ──────────────────────────────────────────────
    top5      = all_picks[:8]
    pick_rows = ""
    for p in top5:
        d_color   = "#16a34a" if p["direction"] == "OVER" else "#dc2626"
        inj_tag   = ""
        if p.get("injury") and p["injury"].get("warning"):
            inj_tag = ' <span style="color:#d97706;font-size:10px;">&#9888;</span>'
        pick_rows += f"""\
<tr style="border-bottom:1px solid #f3f4f6;">
  <td style="padding:7px 0;font-size:13px;font-weight:600;color:#111827;">{p['player']}{inj_tag}</td>
  <td style="padding:7px 4px;font-size:12px;color:#6b7280;">{p['stat_type']}</td>
  <td style="padding:7px 4px;" align="center">
    <span style="background:{d_color};color:#fff;font-size:11px;font-weight:700;
                 padding:2px 9px;border-radius:12px;white-space:nowrap;">
      {p['direction']} {p['line']}</span>
  </td>
  <td style="padding:7px 0;font-size:13px;font-weight:800;color:{d_color};" align="right">
    {int(p['prob']*100)}%</td>
</tr>"""

    top_picks_html = f"""\
<table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:24px;">
  <tr>
    <td colspan="4" style="padding:0 0 10px;">
      <p style="margin:0;font-size:15px;font-weight:800;color:#1e1b4b;">
        &#127919; Top Individual Picks</p>
    </td>
  </tr>
  {pick_rows}
</table>"""

    # ── Parlay sections ───────────────────────────────────────────────────────
    two_pick   = [p for p in parlays if p["size"] == 2][:3]
    three_pick = [p for p in parlays if p["size"] == 3][:3]

    sections = [context_card, top_picks_html]

    if two_pick:
        sections.append(
            '<p style="margin:0 0 12px;font-size:16px;font-weight:800;color:#1e1b4b;'
            'border-bottom:2px solid #818cf8;padding-bottom:8px;">'
            '&#128176; Best 2-Pick Parlays &nbsp;<span style="color:#6b7280;font-weight:400;'
            'font-size:13px;">3x payout</span></p>'
        )
        for i, p in enumerate(two_pick, 1):
            sections.append(_parlay_card_html(p, i))

    if three_pick:
        sections.append(
            '<p style="margin:20px 0 12px;font-size:16px;font-weight:800;color:#1e1b4b;'
            'border-bottom:2px solid #818cf8;padding-bottom:8px;">'
            '&#128640; Best 3-Pick Parlays &nbsp;<span style="color:#6b7280;font-weight:400;'
            'font-size:13px;">5x payout</span></p>'
        )
        for i, p in enumerate(three_pick, 1):
            sections.append(_parlay_card_html(p, len(two_pick) + i))

    if not parlays:
        sections.append(
            '<p style="text-align:center;color:#6b7280;font-size:14px;padding:20px 0;">'
            'No high-confidence parlays found tonight. Consider skipping.</p>'
        )

    # How-to footer
    sections.append(f"""\
<table width="100%" cellpadding="0" cellspacing="0"
       style="margin-top:16px;border-top:1px solid #e5e7eb;">
  <tr><td style="padding:14px 0 0;">
    <p style="margin:0 0 6px;font-size:12px;font-weight:700;color:#374151;">
      How PrizePicks Payouts Work</p>
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td style="text-align:center;padding:6px 4px;background:#f9fafb;border-radius:6px;">
          <p style="margin:0;font-size:10px;color:#9ca3af;text-transform:uppercase;">2-Pick</p>
          <p style="margin:2px 0 0;font-size:14px;font-weight:800;color:#374151;">3x</p>
          <p style="margin:1px 0 0;font-size:10px;color:#6b7280;">57.7% break-even</p>
        </td>
        <td width="6"></td>
        <td style="text-align:center;padding:6px 4px;background:#f9fafb;border-radius:6px;">
          <p style="margin:0;font-size:10px;color:#9ca3af;text-transform:uppercase;">3-Pick</p>
          <p style="margin:2px 0 0;font-size:14px;font-weight:800;color:#374151;">5x</p>
          <p style="margin:1px 0 0;font-size:10px;color:#6b7280;">58.5% break-even</p>
        </td>
        <td width="6"></td>
        <td style="text-align:center;padding:6px 4px;background:#f9fafb;border-radius:6px;">
          <p style="margin:0;font-size:10px;color:#9ca3af;text-transform:uppercase;">4-Pick</p>
          <p style="margin:2px 0 0;font-size:14px;font-weight:800;color:#374151;">10x</p>
          <p style="margin:1px 0 0;font-size:10px;color:#6b7280;">56.2% break-even</p>
        </td>
        <td width="6"></td>
        <td style="text-align:center;padding:6px 4px;background:#f9fafb;border-radius:6px;">
          <p style="margin:0;font-size:10px;color:#9ca3af;text-transform:uppercase;">5-Pick</p>
          <p style="margin:2px 0 0;font-size:14px;font-weight:800;color:#374151;">20x</p>
          <p style="margin:1px 0 0;font-size:10px;color:#6b7280;">55.0% break-even</p>
        </td>
        <td width="6"></td>
        <td style="text-align:center;padding:6px 4px;background:#f9fafb;border-radius:6px;">
          <p style="margin:0;font-size:10px;color:#9ca3af;text-transform:uppercase;">6-Pick</p>
          <p style="margin:2px 0 0;font-size:14px;font-weight:800;color:#374151;">25x</p>
          <p style="margin:1px 0 0;font-size:10px;color:#6b7280;">54.0% break-even</p>
        </td>
      </tr>
    </table>
    <p style="margin:10px 0 0;font-size:11px;color:#9ca3af;line-height:1.6;">
      Confidence % = estimated hit probability based on L5 hit rate, average vs line,
      season trend, minutes, and injury status. Not a guarantee. Always verify lines
      in the PP app — lines can move up to tip-off.
    </p>
  </td></tr>
</table>""")

    body = "\n".join(sections)

    html = f"""\
<!DOCTYPE html><html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:16px;background:#0f172a;font-family:Arial,Helvetica,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">
<table width="620" cellpadding="0" cellspacing="0" style="max-width:620px;">

  <!-- Header -->
  <tr>
    <td style="background:#1e1b4b;padding:24px 28px;border-radius:12px 12px 0 0;">
      <p style="margin:0 0 4px;color:#fbbf24;font-size:12px;font-weight:700;
                text-transform:uppercase;letter-spacing:2px;">
        PrizePicks Parlay Report &nbsp;&#8226;&nbsp; NBA Playoffs</p>
      <p style="margin:0 0 4px;color:#ffffff;font-size:28px;font-weight:800;line-height:1.2;">
        {game_header}</p>
      <p style="margin:0;color:#a5b4fc;font-size:14px;">
        {tip_str} &nbsp;&#8226;&nbsp; {mins_str}</p>
    </td>
  </tr>

  <!-- Body -->
  <tr>
    <td style="background:#ffffff;padding:24px 28px;border-radius:0 0 12px 12px;">
      {body}
    </td>
  </tr>

  <!-- Footer -->
  <tr>
    <td style="padding:12px 0 0;">
      <p style="margin:0;color:#64748b;font-size:11px;text-align:center;">
        Kalshi Bot &nbsp;&#8226;&nbsp; Automated pre-game analysis &nbsp;&#8226;&nbsp; Not financial advice
      </p>
    </td>
  </tr>

</table>
</td></tr></table>
</body></html>"""

    # Plain text version
    plain_lines = [
        f"PP Parlay Report: {game_header} — {tip_str}",
        f"Generated: {ts}",
        "",
        "TOP PICKS:",
    ]
    for p in all_picks[:8]:
        plain_lines.append(
            f"  {p['player']} {p['direction']} {p['line']} {p['stat_type']} — {int(p['prob']*100)}%"
        )
    plain_lines.append("")
    plain_lines.append("PARLAYS:")
    for i, p in enumerate(parlays[:6], 1):
        legs = " + ".join(
            f"{pk['player'].split()[-1]} {pk['direction']} {pk['line']}"
            for pk in p["picks"]
        )
        plain_lines.append(
            f"  #{i} {p['size']}-pick ({p['payout']}x): {legs} | {int(p['combined']*100)}% combined"
        )

    plain = "\n".join(plain_lines)
    return subject, html, plain


# ── Sent-report tracking ───────────────────────────────────────────────────────

def _load_sent() -> dict:
    if SENT_LOG_PATH.exists():
        try:
            return json.loads(SENT_LOG_PATH.read_text())
        except Exception:
            pass
    return {}


def _save_sent(sent: dict):
    SENT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    SENT_LOG_PATH.write_text(json.dumps(sent))


# ── Entry Points ───────────────────────────────────────────────────────────────

def run_now(games: list[dict]):
    """Run the full analysis and send the email for the given game list."""
    if not games:
        _log("[report] No games provided — nothing to do")
        return

    _log(f"[report] Analysing {len(games)} game(s): " + ", ".join(g['name'] for g in games))

    # 1. PP lines
    projections = fetch_pp_projections()
    if not projections:
        _log("[report] No PP projections found — aborting")
        return

    # 2. Injuries
    try:
        injury_report = get_injury_report()
        _log(f"[injuries] {len(injury_report)} players on report")
    except Exception as e:
        _log(f"[injuries] Failed: {e}")
        injury_report = {}

    # 3. Score picks
    _log(f"[picks] Scoring {len(projections)} projections (fetching NBA stats — ~30s)...")
    all_picks = score_all_picks(projections, injury_report)
    _log(f"[picks] {len(all_picks)} picks above {int(MIN_PICK_PROB*100)}% threshold")

    if not all_picks:
        _log("[report] No qualifying picks — skipping email")
        return

    # Log picks to hit tracker
    try:
        from hit_tracker import log_picks
        game_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log_picks(all_picks, game_date)
    except Exception as e:
        _log(f"[hit_tracker] log_picks error (non-fatal): {e}")

    # 4. Build parlays
    parlays = build_parlays(all_picks)
    _log(f"[parlays] {len(parlays)} parlay combos (top EV: {parlays[0]['ev']:.2f})" if parlays else "[parlays] None found")

    # 5. Format & send
    subject, html, plain = format_report_email(parlays, all_picks, games)

    push_lines = []
    for p in parlays[:2]:
        legs = " + ".join(f"{pk['player'].split()[-1]} {pk['direction']}" for pk in p["picks"])
        push_lines.append(f"{p['size']}-pick ({p['payout']}x): {legs} | {int(p['combined']*100)}%")
    push_body = " || ".join(push_lines) if push_lines else "No strong parlays tonight"

    send_push(push_body, title=f"🏀 PP Report: {games[0]['name']}")
    send_email(subject, html, plain)
    _log(f"[report] Email sent: {subject}")


def run(force: bool = False):
    """
    Check today's NBA games and fire the pre-game report ~3 hours before tip-off.
    Called by scheduler.py every 30 minutes.

    force=True: send for ALL upcoming games regardless of timing (for manual runs).
    """
    # Resolve yesterday's picks at the start of each run
    try:
        from hit_tracker import resolve_yesterday_picks
        resolve_yesterday_picks()
    except Exception as e:
        _log(f"[hit_tracker] resolve error (non-fatal): {e}")

    games = get_todays_games()
    if not games:
        _log("[report] No NBA games today")
        return

    sent  = _load_sent()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for game in games:
        mins    = game["mins_until"]
        gid     = game["game_id"] or game["name"]
        key     = f"{today}|{gid}"

        in_window    = REPORT_WINDOW_MIN[0] <= mins <= REPORT_WINDOW_MIN[1]
        already_sent = key in sent

        if force or (in_window and not already_sent):
            _log(f"[report] {game['name']} tips in {mins:.0f}m — running report")
            run_now([game])
            sent[key] = datetime.now(timezone.utc).isoformat()
            _save_sent(sent)
        else:
            status = "already sent" if already_sent else f"{mins:.0f}m away (outside window)"
            _log(f"[report] {game['name']} — {status}")


if __name__ == "__main__":
    force = "--force" in sys.argv
    if force:
        games = get_todays_games()
        if games:
            run_now(games)
        else:
            print("No NBA games found today.")
    else:
        run()
