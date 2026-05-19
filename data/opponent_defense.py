"""
Opponent defensive rankings for NBA, WNBA, and MLB.

NBA: stats.nba.com team defense (pts/reb/ast allowed per game)
WNBA: ESPN team stats
MLB: statsapi.mlb.com team pitching ERA and opponent slash line

get_opponent_context(sport, opponent_team, stat_type) -> dict | None
  Returns {rank, n_teams, label, prob_adjustment, stat_value, description}
  rank=1 means best defense (hardest for offense), rank=n_teams means worst
  prob_adjustment: float added to pick probability (-0.05 to +0.05)
"""

import json
import time
from pathlib import Path

import requests

NBA_CACHE_PATH  = Path("logs/.nba_defense_cache.json")
MLB_CACHE_PATH  = Path("logs/.mlb_defense_cache.json")
WNBA_CACHE_PATH = Path("logs/.wnba_defense_cache.json")
CACHE_TTL       = 3600  # 1 hour

NBA_HEADERS = {
    "User-Agent":          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer":             "https://www.nba.com/",
    "Accept":              "application/json",
    "x-nba-stats-origin":  "stats",
    "x-nba-stats-token":   "true",
}

# Mapping from PP stat_type to the NBA opponent stat column name
NBA_STAT_COL_MAP = {
    "Points":    "OPP_PTS",
    "Rebounds":  "OPP_REB",
    "Assists":   "OPP_AST",
    "3-PT Made": "OPP_FG3M",
    "Blocks":    "OPP_BLK",
    "Steals":    "OPP_STL",
    "Turnovers": "OPP_TOV",
}

# Combined stat types that use Points as the primary defensive signal
COMBINED_NBA_MAP = {
    "Pts+Reb+Ast": "OPP_PTS",
    "Pts+Reb":     "OPP_PTS",
    "Pts+Ast":     "OPP_PTS",
}

MLB_BATTER_STATS  = {"Hits", "Total Bases", "Runs", "RBIs", "Home Runs",
                      "Stolen Bases", "Hitter Strikeouts", "Singles", "Doubles",
                      "Walks", "Hits+Runs+RBIs"}
MLB_PITCHER_STATS = {"Pitcher Strikeouts"}


# ── Cache helpers ──────────────────────────────────────────────────────────────

def _load_cache(path: Path) -> dict:
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return {}


def _save_cache(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _cache_fresh(cache: dict, key: str) -> bool:
    entry = cache.get(key, {})
    return bool(entry) and (time.time() - entry.get("ts", 0)) < CACHE_TTL


# ── Team name matching ─────────────────────────────────────────────────────────

def _last_word(name: str) -> str:
    return name.strip().lower().split()[-1] if name.strip() else ""


def _find_team(defense_map: dict, team_name: str):
    """Fuzzy-match team name (last word) against a defense_map keyed by team name."""
    needle = _last_word(team_name)
    for team_key, val in defense_map.items():
        if _last_word(team_key) == needle:
            return team_key, val
    return None, None


# ── NBA Defense ────────────────────────────────────────────────────────────────

def _fetch_nba_defense() -> dict:
    """
    Fetch NBA opponent stats from stats.nba.com.
    Returns dict: {team_name: {OPP_PTS, OPP_REB, OPP_AST, OPP_FG3M, ...}}
    """
    cache = _load_cache(NBA_CACHE_PATH)
    if _cache_fresh(cache, "data"):
        return cache["data"]["teams"]

    url = "https://stats.nba.com/stats/leaguedashteamstats"
    params = {
        "Season":     "2024-25",
        "SeasonType": "Playoffs",
        "PerMode":    "PerGame",
        "MeasureType": "Opponent",
        "LeagueID":   "00",
    }

    try:
        resp = requests.get(url, headers=NBA_HEADERS, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        # Fallback to regular season if playoffs data unavailable
        try:
            params["SeasonType"] = "Regular Season"
            resp = requests.get(url, headers=NBA_HEADERS, params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return {}

    try:
        result_set = data["resultSets"][0]
        headers    = result_set["headers"]
        rows       = result_set["rowSet"]
    except (KeyError, IndexError):
        return {}

    teams = {}
    for row in rows:
        row_dict  = dict(zip(headers, row))
        team_name = row_dict.get("TEAM_NAME", "")
        if team_name:
            teams[team_name] = row_dict

    cache_data = {"ts": time.time(), "teams": teams}
    _save_cache(NBA_CACHE_PATH, {"data": cache_data})
    return teams


# ── WNBA Defense ───────────────────────────────────────────────────────────────

def _fetch_wnba_defense() -> dict:
    """
    Fetch WNBA team stats from ESPN.
    Returns dict: {team_name: {pts_allowed, reb_allowed, ast_allowed}}
    """
    cache = _load_cache(WNBA_CACHE_PATH)
    if _cache_fresh(cache, "data"):
        return cache["data"]["teams"]

    teams = {}

    try:
        # Get all WNBA teams
        resp = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/teams",
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            timeout=15,
        )
        resp.raise_for_status()
        team_list = resp.json().get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", [])
    except Exception:
        return {}

    for entry in team_list:
        team_info = entry.get("team", {})
        team_id   = team_info.get("id", "")
        team_name = team_info.get("displayName", "")
        if not team_id:
            continue

        try:
            sr = requests.get(
                f"https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/teams/{team_id}/statistics",
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
                timeout=10,
            )
            sr.raise_for_status()
            stats_data = sr.json()

            # Parse stats categories
            team_stats = {}
            for cat in stats_data.get("results", []):
                for stat in cat.get("stats", []):
                    team_stats[stat.get("name", "")] = stat.get("value", 0)

            teams[team_name] = team_stats
            time.sleep(0.2)
        except Exception:
            continue

    cache_data = {"ts": time.time(), "teams": teams}
    _save_cache(WNBA_CACHE_PATH, {"data": cache_data})
    return teams


# ── MLB Defense ────────────────────────────────────────────────────────────────

def _fetch_mlb_defense() -> dict:
    """
    Fetch MLB team pitching stats from statsapi.
    Returns dict: {team_name: {era, whip, strikeoutsPerNineInnings}}
    """
    cache = _load_cache(MLB_CACHE_PATH)
    if _cache_fresh(cache, "data"):
        return cache["data"]["teams"]

    # Get all MLB teams
    try:
        resp = requests.get(
            "https://statsapi.mlb.com/api/v1/teams",
            params={"sportId": 1, "season": 2026},
            timeout=15,
        )
        resp.raise_for_status()
        all_teams = resp.json().get("teams", [])
    except Exception:
        return {}

    teams = {}
    for team in all_teams:
        team_id   = team.get("id")
        team_name = team.get("name", "")
        if not team_id or not team_name:
            continue

        team_data: dict = {}

        # Pitching stats (for hitter prop opponent context)
        try:
            sr = requests.get(
                f"https://statsapi.mlb.com/api/v1/teams/{team_id}/stats",
                params={"stats": "season", "group": "pitching", "season": 2026},
                timeout=10,
            )
            sr.raise_for_status()
            splits = sr.json().get("stats", [{}])[0].get("splits", [{}])
            if splits:
                stat = splits[0].get("stat", {})
                team_data["era"]  = float(stat.get("era",  99))
                team_data["whip"] = float(stat.get("whip", 99))
                team_data["pitching_k9"] = float(stat.get("strikeoutsPer9Inn", 0))
        except Exception:
            pass

        # Batting stats (for pitcher K prop opponent context — how often does this lineup K?)
        try:
            br = requests.get(
                f"https://statsapi.mlb.com/api/v1/teams/{team_id}/stats",
                params={"stats": "season", "group": "hitting", "season": 2026},
                timeout=10,
            )
            br.raise_for_status()
            splits = br.json().get("stats", [{}])[0].get("splits", [{}])
            if splits:
                stat = splits[0].get("stat", {})
                ks   = int(stat.get("strikeOuts", 0))
                pas  = int(stat.get("plateAppearances", 1))
                team_data["batting_k_pct"] = round(ks / max(pas, 1), 4)
        except Exception:
            pass

        if team_data:
            teams[team_name] = team_data
        time.sleep(0.1)

    cache_data = {"ts": time.time(), "teams": teams}
    _save_cache(MLB_CACHE_PATH, {"data": cache_data})
    return teams


# ── Ranking helpers ────────────────────────────────────────────────────────────

def _rank_teams(defense_map: dict, stat_key: str, lower_is_better: bool = True) -> dict:
    """
    Rank all teams by the given stat. Returns {team_name: rank} where rank=1 = best defense.
    lower_is_better=True means lower stat value = better defense (e.g., ERA, pts allowed).
    """
    values = []
    for team_name, stats in defense_map.items():
        val = stats.get(stat_key)
        if val is not None:
            try:
                values.append((team_name, float(val)))
            except (TypeError, ValueError):
                pass

    # Sort: ascending if lower_is_better (lower = best defense = rank 1)
    values.sort(key=lambda x: x[1], reverse=not lower_is_better)
    return {team_name: rank + 1 for rank, (team_name, _) in enumerate(values)}


def _get_defense_label(rank: int, n_teams: int) -> str:
    pct = rank / n_teams
    if pct <= 0.17:
        return "Elite Defense"
    elif pct <= 0.35:
        return "Strong Defense"
    elif pct <= 0.65:
        return "Average Defense"
    elif pct <= 0.83:
        return "Weak Defense"
    else:
        return "Terrible Defense"


def _get_prob_adjustment(rank: int, n_teams: int, direction: str) -> float:
    """
    Map defensive rank to probability adjustment.
    Top-5 defense (hardest): OVER -0.04, UNDER +0.04
    Top-10: OVER -0.02, UNDER +0.02
    Bottom-5: OVER +0.04, UNDER -0.04
    Bottom-10: OVER +0.02, UNDER -0.02
    """
    is_over = direction.upper() == "OVER"

    if rank <= 5:
        adj = -0.04 if is_over else 0.04
    elif rank <= 10:
        adj = -0.02 if is_over else 0.02
    elif rank >= n_teams - 4:
        adj = 0.04 if is_over else -0.04
    elif rank >= n_teams - 9:
        adj = 0.02 if is_over else -0.02
    else:
        adj = 0.0

    return adj


# ── Public API ─────────────────────────────────────────────────────────────────

def get_opponent_context(
    sport: str,
    opponent_team: str,
    stat_type: str,
    direction: str = "OVER",
) -> dict | None:
    """
    Return defensive context for the opposing team relative to the given stat.

    Returns dict:
      {rank, n_teams, label, prob_adjustment, stat_value, description}
    Returns None if data unavailable or team not found.
    """
    try:
        if sport == "NBA":
            return _get_nba_context(opponent_team, stat_type, direction)
        elif sport == "WNBA":
            return _get_wnba_context(opponent_team, stat_type, direction)
        elif sport == "MLB":
            return _get_mlb_context(opponent_team, stat_type, direction)
    except Exception:
        pass
    return None


def _get_nba_context(opponent_team: str, stat_type: str, direction: str) -> dict | None:
    defense = _fetch_nba_defense()
    if not defense:
        return None

    # Determine which column to use
    stat_col = NBA_STAT_COL_MAP.get(stat_type) or COMBINED_NBA_MAP.get(stat_type)
    if not stat_col:
        return None

    team_key, team_stats = _find_team(defense, opponent_team)
    if not team_stats:
        return None

    stat_value = team_stats.get(stat_col)
    if stat_value is None:
        return None

    # Build a simple value map for ranking
    stat_map = {name: {stat_col: stats.get(stat_col, 0)} for name, stats in defense.items()}
    ranks    = _rank_teams(stat_map, stat_col, lower_is_better=True)
    rank     = ranks.get(team_key, 0)
    n_teams  = len(ranks)
    label    = _get_defense_label(rank, n_teams)
    adj      = _get_prob_adjustment(rank, n_teams, direction)

    stat_label_map = {
        "OPP_PTS": "pts/game",
        "OPP_REB": "reb/game",
        "OPP_AST": "ast/game",
        "OPP_FG3M": "3PM/game",
    }
    unit = stat_label_map.get(stat_col, "per game")

    return {
        "rank":            rank,
        "n_teams":         n_teams,
        "label":           label,
        "prob_adjustment": adj,
        "stat_value":      round(float(stat_value), 1),
        "description":     f"Allows {stat_value:.1f} {unit} ({_ordinal(rank)} best defense)",
    }


def _get_wnba_context(opponent_team: str, stat_type: str, direction: str) -> dict | None:
    defense = _fetch_wnba_defense()
    if not defense:
        return None

    team_key, team_stats = _find_team(defense, opponent_team)
    if not team_stats:
        return None

    # Use points allowed as the primary signal for most stats
    stat_key   = "avgPointsAllowed" if "Points" in stat_type or "Pts" in stat_type else "avgPointsAllowed"
    stat_value = team_stats.get(stat_key)
    if stat_value is None:
        return None

    stat_map = {name: {stat_key: stats.get(stat_key, 0)} for name, stats in defense.items()}
    ranks    = _rank_teams(stat_map, stat_key, lower_is_better=True)
    rank     = ranks.get(team_key, 0)
    n_teams  = len(ranks)
    label    = _get_defense_label(rank, n_teams)
    adj      = _get_prob_adjustment(rank, n_teams, direction)

    return {
        "rank":            rank,
        "n_teams":         n_teams,
        "label":           label,
        "prob_adjustment": adj,
        "stat_value":      round(float(stat_value), 1),
        "description":     f"Allows {stat_value:.1f} pts/game ({_ordinal(rank)} best defense)",
    }


def _get_mlb_context(opponent_team: str, stat_type: str, direction: str) -> dict | None:
    defense = _fetch_mlb_defense()
    if not defense:
        return None

    team_key, team_stats = _find_team(defense, opponent_team)
    if not team_stats:
        return None

    if stat_type in MLB_PITCHER_STATS:
        # For pitcher K props: use opposing lineup's BATTING strikeout rate
        # High batting K% = lineup strikes out a lot = easier for pitcher to get Ks
        stat_key        = "batting_k_pct"
        lower_is_better = False  # higher K% = easier for pitcher OVER
        unit            = "batter K%"

        stat_value = team_stats.get(stat_key)
        if stat_value is None:
            return None

        stat_map = {name: {stat_key: stats.get(stat_key, 0)} for name, stats in defense.items()}
        ranks    = _rank_teams(stat_map, stat_key, lower_is_better=False)
        rank     = ranks.get(team_key, 0)
        n_teams  = len(ranks)
        # Flip label logic: high K% lineup = easy matchup for pitcher = "Weak" contact
        label    = _get_defense_label(n_teams - rank + 1, n_teams)  # invert rank for label
        adj      = _get_prob_adjustment(rank, n_teams, direction)

        return {
            "rank":            rank,
            "n_teams":         n_teams,
            "label":           label,
            "prob_adjustment": adj,
            "stat_value":      round(float(stat_value), 4),
            "description":     (
                f"Opposing lineup K% {stat_value:.1%} "
                f"({_ordinal(rank)} most Ks in MLB — "
                f"{'easy' if rank >= n_teams * 0.6 else 'tough'} matchup for pitcher Ks)"
            ),
        }

    else:
        # For hitter props: use opposing team's ERA + tonight's probable pitcher
        stat_key        = "era"
        lower_is_better = True

        stat_value = team_stats.get(stat_key)
        if stat_value is None:
            return None

        stat_map = {name: {"era": stats.get("era", 99)} for name, stats in defense.items()}
        ranks    = _rank_teams(stat_map, "era", lower_is_better=True)
        rank     = ranks.get(team_key, 0)
        n_teams  = len(ranks)
        label    = _get_defense_label(rank, n_teams)
        adj      = _get_prob_adjustment(rank, n_teams, direction)

        desc = f"Team ERA {stat_value:.2f} ({_ordinal(rank)} best pitching staff)"

        # Also check tonight's probable starter for extra context
        try:
            from data.lineups import get_mlb_probable_pitcher
            pitcher_name, pitcher_era = get_mlb_probable_pitcher(opponent_team) or (None, None)
            if pitcher_name and pitcher_era is not None:
                pitcher_era = float(pitcher_era)
                desc += f" • Starter: {pitcher_name} ({pitcher_era:.2f} ERA)"
                # Additional adjustment for elite/terrible starters
                if pitcher_era < 2.50:
                    extra = -0.03 if direction.upper() == "OVER" else 0.03
                    adj   = round(adj + extra, 3)
                    desc += " — ace, tough night for hitters"
                elif pitcher_era < 3.50:
                    extra = -0.02 if direction.upper() == "OVER" else 0.02
                    adj   = round(adj + extra, 3)
                elif pitcher_era > 6.00:
                    extra = 0.03 if direction.upper() == "OVER" else -0.03
                    adj   = round(adj + extra, 3)
                    desc += " — struggling starter, good for hitters"
                elif pitcher_era > 5.00:
                    extra = 0.02 if direction.upper() == "OVER" else -0.02
                    adj   = round(adj + extra, 3)
        except Exception:
            pass

        return {
            "rank":            rank,
            "n_teams":         n_teams,
            "label":           label,
            "prob_adjustment": round(adj, 3),
            "stat_value":      round(float(stat_value), 2),
            "description":     desc,
        }


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"
