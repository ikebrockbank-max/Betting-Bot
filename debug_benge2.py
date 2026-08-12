"""Nail the Benge DNP bug: (1) are there multiple MLB players named Benge
(collision)? (2) does pid 701807's recent game log show REAL plate appearances
or phantom/DNP entries (an entry with 0 PA that shouldn't grade as played)?"""
import json, urllib.request


def get(u):
    return json.loads(urllib.request.urlopen(u, timeout=12).read())


print("=== MLB players named 'Benge' (collision check) ===")
res = get("https://statsapi.mlb.com/api/v1/people/search?names=Benge")
for p in res.get("people", []):
    print(f"  {p.get('id')} {p.get('fullName')} debut={p.get('mlbDebutDate')} "
          f"pos={p.get('primaryPosition',{}).get('abbreviation')} active={p.get('active')}")

# also try the search the resolver likely uses
res2 = get("https://statsapi.mlb.com/api/v1/people/search?names=Carson%20Benge")
print("  search 'Carson Benge':", [(p['id'], p['fullName']) for p in res2.get("people", [])])

print("\n=== pid 701807 recent game log — FULL (PA/AB to spot phantom/DNP entries) ===")
gl = get("https://statsapi.mlb.com/api/v1/people/701807/stats?stats=gameLog&group=hitting&season=2026")
splits = gl.get("stats", [{}])[0].get("splits", [])
print(f"total entries: {len(splits)}")
for s in splits[-6:]:
    st = s.get("stat", {})
    print(f"  {s.get('date')} game={s.get('game',{}).get('gamePk')} "
          f"PA={st.get('plateAppearances')} AB={st.get('atBats')} "
          f"H={st.get('hits')} R={st.get('runs')} RBI={st.get('rbi')} "
          f"HR={st.get('homeRuns')} BB={st.get('baseOnBalls')} "
          f"isWin?={s.get('isWin')} team={s.get('team',{}).get('abbreviation')}")
