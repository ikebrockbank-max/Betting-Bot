"""One cheap call to read the RapidAPI daily quota remaining — to confirm
whether today's 0 picks is budget exhaustion (from heavy testing) vs a bug."""
import os, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta
KEY = os.getenv("RAPIDAPI_KEY", "")
HOST = "wnba-api.p.rapidapi.com"
H = {"x-rapidapi-host": HOST, "x-rapidapi-key": KEY}
et = datetime.now(timezone.utc) - timedelta(hours=4)
url = f"https://{HOST}/wnbaschedule?year={et:%Y}&month={et:%m}&day={et:%d}"
try:
    with urllib.request.urlopen(urllib.request.Request(url, headers=H), timeout=20) as r:
        rl = {k: v for k, v in r.headers.items() if "ratelimit" in k.lower()}
        print("status 200; quota headers:", rl)
except urllib.error.HTTPError as e:
    rl = {k: v for k, v in e.headers.items() if "ratelimit" in k.lower()}
    print(f"HTTP {e.code}; quota headers:", rl)
