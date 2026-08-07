"""Probe stats.wnba.com reachability FROM THE RUNNER (datacenter IP) — the
env where the WNBA-hot tier actually runs and where ESPN is IP-blocked.
Tests the 3 endpoints a stats.wnba.com integration would need, with retries,
and reports success + latency so we know if Option B2 is viable."""
import urllib.request, json, time
from datetime import datetime, timezone, timedelta

H = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Referer": "https://www.wnba.com/", "Origin": "https://www.wnba.com",
    "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9",
    "x-nba-stats-origin": "stats", "x-nba-stats-token": "true", "Connection": "keep-alive",
}


def get(url, retries=5, to=25):
    last = None
    for a in range(retries):
        t = time.time()
        try:
            r = json.loads(urllib.request.urlopen(
                urllib.request.Request(url, headers=H), timeout=to).read())
            return r, time.time() - t, a + 1
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            time.sleep(2 + a)
    return None, 0, last


today = (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%m/%d/%Y")
tests = [
    ("scoreboardv2", f"https://stats.wnba.com/stats/scoreboardv2?GameDate={today}&LeagueID=10&DayOffset=0"),
    ("commonteamroster", "https://stats.wnba.com/stats/commonteamroster?LeagueID=10&Season=2026&TeamID=1611661317"),
    ("playergamelog", "https://stats.wnba.com/stats/playergamelog?LeagueID=10&Season=2026&SeasonType=Regular+Season&PlayerID=1628886"),
]
ok = 0
for name, url in tests:
    d, secs, info = get(url)
    if d:
        rs = d.get("resultSets", [{}])
        n = len(rs[0].get("rowSet", [])) if rs else 0
        print(f"✅ {name}: OK in {secs:.1f}s (attempt {info}), rows={n}")
        ok += 1
    else:
        print(f"❌ {name}: FAILED after retries — {info}")
print(f"\n{ok}/{len(tests)} endpoints reachable from runner")
