"""End-to-end test of the revived WNBA-hot tier on RapidAPI. Prints the index
size, how many combos are home, and the final picks — so we confirm the tier
produces real output before it goes into the live brief."""
from data import wnba_rapidapi as w
from daily_top_picks import _find_wnba_hot

print("RAPIDAPI_KEY present:", w.available())
idx = w.build_player_index()
print("player index size (players on teams playing today):", len(idx))
sample = list(idx.items())[:3]
for k, v in sample:
    print("  ", k, "->", v)

picks = _find_wnba_hot(n=5)
print(f"\nWNBA-hot picks: {len(picks)}")
for p in picks:
    print(f"  🏀 {p['player']} OVER {p['line']} {p['stat_type']} "
          f"| p_over={p.get('p_over')} trend={p.get('trend')} "
          f"home={p.get('home_away')} vs {p.get('opp_team')} "
          f"L10={p.get('recent_values')}")
