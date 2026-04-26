"""
injury_alert.py — real-time injury speed alert system.

Polls ESPN every POLL_INTERVAL seconds and cross-references against open
Kalshi NBA prop markets. When a key player is newly confirmed OUT or
Doubtful, fires an immediate push + email BEFORE Kalshi reprices.

The edge: Kalshi markets sometimes take 5-15 minutes to reprice after
an injury report drops. Betting NO on a scratched player's prop is
nearly risk-free when they can't play.

Run standalone:   python3 injury_alert.py
Scheduler:        import injury_alert; injury_alert.run_once()
"""

import json
import re
import sys
import time
from datetime import datetime, UTC
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from data.injuries import get_injury_report
from data import kalshi
from notify import send_push, send_email

POLL_INTERVAL   = 120        # seconds between polls
SEEN_PATH       = Path("logs/.seen_injuries.json")
LOG_PATH        = Path("logs/injury_alerts.log")

# If a scratched player's YES price is still THIS high, Kalshi hasn't repriced yet
# An OUT player's prop should converge toward ~5-10¢. Above this = stale price.
REPRICE_THRESHOLD = 0.25     # 25¢ — if still above this after injury, it's an alert

# Series tickers to scan for NBA player props
NBA_SERIES = ["KXNBAPTS", "KXNBA3PT", "KXNBAAST", "KXNBAREB", "KXNBATOV"]

# Kalshi team codes (3-letter) → team name for display
TEAM_MAP = {
    "ATL": "Atlanta Hawks", "BKN": "Brooklyn Nets", "BOS": "Boston Celtics",
    "CHA": "Charlotte Hornets", "CHI": "Chicago Bulls", "CLE": "Cleveland Cavaliers",
    "DAL": "Dallas Mavericks", "DEN": "Denver Nuggets", "DET": "Detroit Pistons",
    "GSW": "Golden State Warriors", "HOU": "Houston Rockets", "IND": "Indiana Pacers",
    "LAC": "Los Angeles Clippers", "LAL": "Los Angeles Lakers", "MEM": "Memphis Grizzlies",
    "MIA": "Miami Heat", "MIL": "Milwaukee Bucks", "MIN": "Minnesota Timberwolves",
    "NOP": "New Orleans Pelicans", "NYK": "New York Knicks", "OKC": "Oklahoma City Thunder",
    "ORL": "Orlando Magic", "PHI": "Philadelphia 76ers", "PHX": "Phoenix Suns",
    "POR": "Portland Trail Blazers", "SAC": "Sacramento Kings", "SAS": "San Antonio Spurs",
    "TOR": "Toronto Raptors", "UTA": "Utah Jazz", "WAS": "Washington Wizards",
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _log(msg: str):
    ts   = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def _load_seen() -> dict:
    """Load seen injuries dict: {player_lower: status_when_alerted}"""
    if SEEN_PATH.exists():
        try:
            return json.loads(SEEN_PATH.read_text())
        except Exception:
            pass
    return {}


def _save_seen(seen: dict):
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps(seen))


def _extract_player_name(title: str) -> str | None:
    """
    Extract player name from a Kalshi market title like:
      "Shai Gilgeous-Alexander: 25+ points"
      "Nikola Jokić: 30+ points"
      "Mikal Bridges: 4+ rebounds"
    Returns lowercase player name or None.
    """
    # Title format: "Player Name: N+ stat"
    m = re.match(r"^([^:]+):", title)
    if m:
        return m.group(1).strip().lower()
    return None


def _fetch_nba_markets() -> list[dict]:
    """Fetch open Kalshi NBA prop markets across all tracked stat types."""
    markets = []
    seen = set()
    for prefix in NBA_SERIES:
        try:
            r = kalshi.get("/markets", {"limit": 200, "status": "open", "series_ticker": prefix})
            for m in r.get("markets", []):
                ticker = m.get("ticker", "")
                if ticker and ticker not in seen:
                    seen.add(ticker)
                    markets.append(m)
        except Exception as e:
            _log(f"[warn] Failed to fetch {prefix}: {e}")
    return markets


def _format_injury_email(alerts: list[dict]) -> tuple[str, str, str]:
    """Build injury alert email (push + email)."""
    from notify import _SIMPLE_WRAP, _SIMPLE_CARD, _simple_row

    count = len(alerts)
    subject = f"🚑 {count} Injury Alert{'s' if count > 1 else ''} — Kalshi Still Priced!"

    cards_html = ""
    plain_lines = [subject, ""]

    for a in alerts:
        player   = a["player_name"]
        status   = a["status"]
        detail   = a["detail"] or "injury"
        headline = a["headline"]
        markets  = a["markets"]

        mkt_rows = ""
        for mkt in markets:
            yes_ask_pct = f"{mkt['yes_ask']*100:.0f}¢"
            url = mkt.get("url", "")
            link = f'<a href="{url}" style="color:#2563eb;text-decoration:none;">{mkt["title"][:50]}</a>'
            mkt_rows += _simple_row(link, f"<b style='color:#dc2626;'>{yes_ask_pct} YES ask</b>", "#dc2626")

        rows = (
            _simple_row("Status", f"<b style='color:#dc2626;'>{status}</b>", "#dc2626")
            + _simple_row("Injury", detail.title(), "#374151")
            + _simple_row("Report", headline[:80], "#374151")
            + _simple_row("Open markets", f"{len(markets)} still priced", "#374151")
            + mkt_rows
        )

        cards_html += _SIMPLE_CARD.format(
            accent="#dc2626",
            action=f"🚑 {player} — {status}",
            subtitle=f"Kalshi hasn't repriced yet — bet NO now",
            rows=rows,
        )

        plain_lines += [
            f"{player} ({status} — {detail})",
            f"  {headline}",
        ]
        for mkt in markets:
            plain_lines.append(f"  → {mkt['title'][:50]} | YES ask: {mkt['yes_ask']*100:.0f}¢")
        plain_lines.append("")

    ts = datetime.now(UTC).strftime("%b %d %Y %H:%M UTC")
    html = _SIMPLE_WRAP.format(
        header_color="#dc2626",
        header_title=f"🚑 Injury Alert — {count} player{'s' if count > 1 else ''} scratched",
        header_sub=f"Kalshi markets still stale • {ts}",
        body=cards_html,
    )
    plain = "\n".join(plain_lines)
    return subject, html, plain


# ── Core logic ─────────────────────────────────────────────────────────────────

def run_once(seen: dict | None = None) -> dict:
    """
    Run a single injury check cycle.
    Returns updated seen dict.
    Pass seen=None to load from disk.
    """
    if seen is None:
        seen = _load_seen()

    # 1. Fetch current injury report
    try:
        report = get_injury_report()
        _log(f"[injuries] {len(report)} players on report")
    except Exception as e:
        _log(f"[injuries] Fetch failed: {e}")
        return seen

    # 2. Fetch open Kalshi NBA markets
    try:
        markets = _fetch_nba_markets()
        _log(f"[kalshi] {len(markets)} open NBA prop markets")
    except Exception as e:
        _log(f"[kalshi] Fetch failed: {e}")
        return seen

    # 3. Build player → markets index
    player_markets: dict[str, list[dict]] = {}
    for m in markets:
        title  = m.get("title", "")
        player = _extract_player_name(title)
        if not player:
            continue
        yes_ask = float(m.get("yes_ask_dollars") or m.get("yes_ask") or 0)
        if yes_ask <= 0:
            continue
        entry = {
            "title":   title,
            "ticker":  m.get("ticker", ""),
            "yes_ask": yes_ask,
            "url":     f"https://kalshi.com/markets/{m.get('ticker','')}",
        }
        player_markets.setdefault(player, []).append(entry)

    # 4. Cross-reference: injured players with open, still-priced markets
    new_alerts = []

    for player_lower, inj in report.items():
        if not (inj["disqualified"] or inj["warning"]):
            continue  # healthy, skip

        # Only alert once per player+status combo
        seen_key = f"{player_lower}|{inj['status']}"
        if seen_key in seen:
            continue

        # Check if this player has open Kalshi markets
        open_mkts = player_markets.get(player_lower, [])
        # Also try last-name matching for "LeBron James" → "james"
        if not open_mkts:
            last = player_lower.split()[-1]
            for k, v in player_markets.items():
                if k.endswith(last) or last in k:
                    open_mkts = v
                    break

        if not open_mkts:
            continue

        # Check which markets haven't repriced (YES still above threshold)
        stale_markets = [
            mkt for mkt in open_mkts
            if mkt["yes_ask"] > REPRICE_THRESHOLD
        ]

        if not stale_markets:
            # Markets already repriced — log but don't alert
            _log(f"[skip] {inj['name']} ({inj['status']}) — markets already repriced")
            seen[seen_key] = "repriced"
            continue

        # 🚨 Alert!
        alert = {
            "player_name": inj["name"],
            "player_lower": player_lower,
            "status": inj["status"],
            "detail": inj["detail"],
            "headline": inj["headline"],
            "markets": stale_markets,
            "disqualified": inj["disqualified"],
        }
        new_alerts.append(alert)
        seen[seen_key] = inj["status"]

        severity = "🚑 OUT" if inj["disqualified"] else "⚠️ QUESTIONABLE"
        _log(
            f"{severity} {inj['name']} ({inj['detail']}) — "
            f"{len(stale_markets)} Kalshi market(s) still priced above {REPRICE_THRESHOLD:.0%}"
        )

    # 5. Fire notifications
    if new_alerts:
        # Sort: OUT/Doubtful first, then Questionable
        new_alerts.sort(key=lambda a: (0 if a["disqualified"] else 1))

        # Push notification
        top = new_alerts[0]
        push_msg = (
            f"{top['player_name']} {top['status']} ({top['detail'] or 'injury'}) — "
            f"{len(top['markets'])} market(s) still priced! BET NO NOW"
        )
        if len(new_alerts) > 1:
            push_msg += f" +{len(new_alerts)-1} more"

        title_tag = "🚑 Player OUT — Kalshi Stale!" if top["disqualified"] else "⚠️ Injury Alert"
        send_push(push_msg, title=title_tag)
        _log(f"Push sent: {push_msg[:100]}")

        # Email
        try:
            subj, html, plain = _format_injury_email(new_alerts)
            send_email(subj, html, plain)
            _log(f"Email sent: {subj}")
        except Exception as e:
            _log(f"Email error (non-fatal): {e}")
    else:
        _log("No new injuries with stale Kalshi pricing.")

    _save_seen(seen)
    return seen


def run():
    """
    Continuous polling loop. Runs run_once() every POLL_INTERVAL seconds.
    Designed to run as a daemon on a VPS or alongside scheduler.py.
    """
    _log(f"Injury alert daemon started — polling every {POLL_INTERVAL}s")
    seen = _load_seen()

    while True:
        try:
            seen = run_once(seen)
        except Exception as e:
            import traceback
            _log(f"ERROR in run_once: {traceback.format_exc()}")
        _log(f"Sleeping {POLL_INTERVAL}s...")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
