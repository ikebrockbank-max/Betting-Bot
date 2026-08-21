"""Why is ERA-under logging 0 picks? Look at ALL Earned Runs Allowed rows the
(general) scanner has logged in the last ~10 days — their park_factor, line,
direction — to see if any fall in the tier's band (park 0.95-1.0, line 2.5,
UNDER). If yes -> _find_era_under has a bug; if none -> the slate/park
distribution shifted and 0 is legit."""
from collections import Counter
from datetime import datetime, timezone, timedelta
from calibration_tracker import _sb_fetch


def _f(r, k):
    try:
        return float(r.get(k) or 0)
    except (TypeError, ValueError):
        return 0.0


since = (datetime.now(timezone.utc) - timedelta(days=12)).strftime("%Y-%m-%d")
rows = _sb_fetch(f"select=player,line,direction,park_factor,result,pick_date,stat_type"
                 f"&sport=eq.MLB&pick_date=gte.{since}")
era = [r for r in rows if (r.get("stat_type") or "").startswith("Earned Runs Allowed")]
print(f"Earned Runs Allowed rows since {since}: {len(era)}")

print("\npark_factor distribution (all ERA rows):")
for pf, c in Counter(round(_f(r, 'park_factor'), 2) for r in era).most_common():
    print(f"  park {pf}: {c}")

print("\ndirection x line:")
for k, c in Counter((r.get('direction'), _f(r, 'line')) for r in era).most_common():
    print(f"  {k}: {c}")

band = [r for r in era if r.get("direction") == "UNDER"
        and _f(r, "line") == 2.5 and 0.95 <= _f(r, "park_factor") < 1.0]
print(f"\n>>> rows in the tier band (UNDER, line 2.5, park 0.95-1.0): {len(band)}")
for r in sorted(band, key=lambda x: x.get("pick_date", "")):
    print(f"  {r.get('pick_date')} {r['player'][:20]:20} park {_f(r,'park_factor'):.2f} "
          f"-> {r.get('result')} | stat={r.get('stat_type')}")
