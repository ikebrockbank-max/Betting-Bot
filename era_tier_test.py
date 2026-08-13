"""Smoke-test the new ERA-under tier in isolation (no ntfy push)."""
from daily_top_picks import _find_era_under

picks = _find_era_under(n=5)
print(f"ERA-under picks today: {len(picks)}")
for p in picks:
    aw = "away" if (p.get("home_away") or "").lower() == "away" else "home"
    print(f"  ⚾ {p['player']} UNDER {p['line']} {p['stat_type']} "
          f"| park {p.get('park_factor')} {aw} vs {p.get('opp_team')} "
          f"dir={p.get('direction')}")
