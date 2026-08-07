"""Find the season param for player-gamelog so we get 2026 (current) games,
not the 2024 default. Brionna Jones (3058895) is active in 2026."""
import os, json, time, urllib.request, urllib.error
KEY = os.getenv("RAPIDAPI_KEY", "")
HOST = "wnba-api.p.rapidapi.com"
H = {"x-rapidapi-host": HOST, "x-rapidapi-key": KEY}
PID = "3058895"


def call(path, pause=6):
    time.sleep(pause)
    try:
        with urllib.request.urlopen(urllib.request.Request(f"https://{HOST}/{path}", headers=H), timeout=25) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:120]
    except Exception as e:
        return None, str(e).encode()


for i, path in enumerate([
    f"player-gamelog?playerId={PID}&season=2026",
    f"player-gamelog?playerId={PID}&year=2026",
    f"player-gamelog?playerId={PID}&seasonYear=2026",
    f"player-gamelog?playerId={PID}&season=2025",
]):
    st, body = call(path, pause=0 if i == 0 else 6)
    tag = f"-> {st}"
    if st == 200:
        try:
            gl = json.loads(body).get("player_gamelog", {})
            sts = [s.get("displayName") for s in gl.get("seasonTypes", [])]
            tag += f"  seasonTypes={sts}"
        except Exception:
            tag += " (parse err)"
    print(f"[{path}] {tag}")
