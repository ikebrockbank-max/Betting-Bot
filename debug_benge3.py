"""Decisive check: does Benge's game-log entry DATE match the actual game's
date? If the split says 2026-08-11 but the gamePk's real date is different,
_fetch_actual is grading the wrong game (phantom/misaligned) — which would let
a real DNP be scored off some other day's stats. Also check the 08-11 boxscore
directly for whether Benge actually appeared."""
import json, urllib.request


def get(u):
    return json.loads(urllib.request.urlopen(u, timeout=12).read())


gl = get("https://statsapi.mlb.com/api/v1/people/701807/stats?stats=gameLog&group=hitting&season=2026")
splits = gl.get("stats", [{}])[0].get("splits", [])
print("split_date -> gamePk -> actual_game_date (MISMATCH?) | Benge H/PA in that game")
for s in splits[-8:]:
    sd = s.get("date")
    pk = s.get("game", {}).get("gamePk")
    st = s.get("stat", {})
    real = "?"
    try:
        g = get(f"https://statsapi.mlb.com/api/v1/schedule?gamePk={pk}")
        real = g.get("dates", [{}])[0].get("date", "?")
    except Exception as e:
        real = f"err {e}"
    flag = "  <== MISMATCH" if real != sd and real != "?" else ""
    print(f"  {sd} -> {pk} -> {real}{flag} | H={st.get('hits')} PA={st.get('plateAppearances')}")

# Direct: did Benge appear in the box score of games on 2026-08-11?
print("\n=== games on 2026-08-11 and whether Benge (701807) appeared ===")
sched = get("https://statsapi.mlb.com/api/v1/schedule?sportId=1&date=2026-08-11")
games = [g["gamePk"] for d in sched.get("dates", []) for g in d.get("games", [])]
print(f"games on 08-11: {len(games)}")
found = False
for pk in games:
    try:
        box = get(f"https://statsapi.mlb.com/api/v1/game/{pk}/boxscore")
        for side in ("home", "away"):
            players = box.get("teams", {}).get(side, {}).get("players", {})
            if "ID701807" in players:
                p = players["ID701807"]
                bat = p.get("stats", {}).get("batting", {})
                print(f"  game {pk}: Benge present, batting PA={bat.get('plateAppearances')} "
                      f"AB={bat.get('atBats')} H={bat.get('hits')} note={p.get('gameStatus',{})}")
                found = True
    except Exception:
        pass
if not found:
    print("  Benge did NOT appear in any 08-11 boxscore -> real DNP")
