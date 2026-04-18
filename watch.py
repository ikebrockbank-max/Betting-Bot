"""
Smart watcher: runs automatically around NBA game times.

Start once and leave it running:
  nohup python3 -u watch.py > logs/watch.log 2>&1 &

What it does:
  1. Fetches today's NBA games from The Odds API
  2. For each game, scans at T-2h, T-30min, T-5min
  3. Auto-resolves results ~2.5h after each tip-off
  4. Sleeps between scans using clock polling (survives Mac sleep)
"""

import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from data import odds
from data.resolve import resolve_edges
from scanner import scan_nba_markets

ET = ZoneInfo("America/New_York")

PRE_GAME_SCAN_OFFSETS = [-120, -30, -5]
RESOLVE_OFFSET = 165


def log(msg: str):
    """Print with flush so nohup log stays current."""
    print(msg, flush=True)


def get_todays_games() -> list[dict]:
    try:
        events = odds.get_events("nba")
    except Exception as e:
        log(f"[watch] Failed to fetch events: {e}")
        return []

    now = datetime.now(timezone.utc)
    games = []
    for event in events:
        tip = datetime.fromisoformat(event["commence_time"].replace("Z", "+00:00"))
        if now < tip + timedelta(hours=4) and tip < now + timedelta(hours=24):
            games.append({
                "id": event["id"],
                "home": event["home_team"],
                "away": event["away_team"],
                "tip_utc": tip,
                "tip_et": tip.astimezone(ET),
            })
    games.sort(key=lambda g: g["tip_utc"])
    return games


def build_schedule(games: list[dict]) -> list[tuple]:
    schedule = []
    for game in games:
        tip = game["tip_utc"]
        label = f"{game['away']} @ {game['home']}"
        for offset in PRE_GAME_SCAN_OFFSETS:
            schedule.append((tip + timedelta(minutes=offset), f"scan T{offset:+d}min {label}", "scan"))
        schedule.append((tip + timedelta(minutes=RESOLVE_OFFSET), f"resolve {label}", "resolve"))
    schedule.sort(key=lambda x: x[0])
    return schedule


def sleep_until(target: datetime, label: str):
    """
    Poll every 60s until wall-clock time reaches target.
    Polling instead of one big sleep() so Mac system sleep
    doesn't cause missed events.
    """
    target_et = target.astimezone(ET)
    log(f"[watch] Sleeping until {target_et.strftime('%I:%M %p ET')} — {label}")
    while True:
        remaining = (target - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(60, remaining))


def run():
    log("\n" + "=" * 60)
    log("KALSHI BOT — AUTO WATCHER")
    log("=" * 60)
    log("Scans and resolves automatically. Ctrl+C to stop.\n")

    while True:
        now_et = datetime.now(ET)
        log(f"[watch] {now_et.strftime('%a %b %d %I:%M %p ET')} — checking schedule...")

        games = get_todays_games()

        if not games:
            log("[watch] No games in next 24h. Sleeping 4 hours...")
            time.sleep(4 * 3600)
            continue

        log(f"[watch] {len(games)} game(s) today:")
        for g in games:
            log(f"  {g['away']} @ {g['home']}  {g['tip_et'].strftime('%I:%M %p ET')}")

        now = datetime.now(timezone.utc)
        upcoming = [(t, label, action) for t, label, action in build_schedule(games) if t > now]

        if not upcoming:
            log("[watch] All events for today already passed. Sleeping until tomorrow 6am ET...")
            tomorrow = (now_et + timedelta(days=1)).replace(hour=6, minute=0, second=0, microsecond=0)
            sleep_until(tomorrow.astimezone(timezone.utc), "next day check")
            continue

        log(f"[watch] {len(upcoming)} event(s) scheduled:")
        for t, label, _ in upcoming:
            log(f"  {t.astimezone(ET).strftime('%I:%M %p ET')}  {label}")

        for run_time, label, action in upcoming:
            sleep_until(run_time, label)
            log(f"\n[watch] Running: {label}")
            try:
                if action == "resolve":
                    resolve_edges()
                else:
                    scan_nba_markets()
            except Exception as e:
                log(f"[watch] ERROR: {e}")

        log("\n[watch] All today's events done.")
        tomorrow = (datetime.now(ET) + timedelta(days=1)).replace(hour=6, minute=0, second=0, microsecond=0)
        sleep_until(tomorrow.astimezone(timezone.utc), "next day check")


if __name__ == "__main__":
    run()
