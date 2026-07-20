"""PFS OVER hit rate by pitcher rest_days — is a long layoff (post-break,
post-IL) a systematic PFS OVER killer via pitch-count limits?"""
from calibration_tracker import _sb_fetch

rows = _sb_fetch("select=rest_days,result,direction,pick_date"
                 "&stat_type=eq.Pitcher Fantasy Score&resolved=eq.true"
                 "&result=neq.void")
rows = [r for r in rows if r.get("result") in ("hit", "miss")]
for d in ("OVER", "UNDER"):
    seg = [r for r in rows if r["direction"] == d]
    print(f"PFS {d} (n={len(seg)}):")
    for lo, hi, label in [(0, 4, "0-3 rest"), (4, 6, "4-5 rest"),
                          (6, 8, "6-7 rest"), (8, 99, "8+ rest")]:
        b = [r for r in seg if r.get("rest_days") is not None
             and lo <= int(r["rest_days"]) < hi]
        if b:
            h = sum(r["result"] == "hit" for r in b)
            print(f"  {label:<10} {h:3d}/{len(b):<4d} = {h/len(b):.1%}")
    nb = [r for r in seg if r.get("rest_days") is None]
    if nb:
        h = sum(r["result"] == "hit" for r in nb)
        print(f"  {'(missing)':<10} {h:3d}/{len(nb):<4d} = {h/len(nb):.1%}")
