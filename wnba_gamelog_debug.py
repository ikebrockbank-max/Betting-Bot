"""Dump raw gamelog structure for affected players to pin the combo-value bug:
why is Pts+Rebs+Asts computing ~7 instead of ~20, and why NO VALS for some?"""
import json
from data import wnba_rapidapi as w

for name in ["Brionna Jones", "Olivia Nelson-Ododa", "Leila Lacan"]:
    info = w.resolve(name)
    print(f"\n===== {name} -> {info} =====")
    if not info:
        continue
    d = w._get(f"player-gamelog?playerId={info['player_id']}")
    if not d:
        print("  gamelog: None")
        continue
    gl = d.get("player_gamelog", {})
    print("  labels:", gl.get("labels"))
    sts = gl.get("seasonTypes", [])
    print("  n seasonTypes:", len(sts))
    for stype in sts[:4]:
        cats = stype.get("categories", [])
        print(f"  seasonType '{stype.get('displayName')}' team={stype.get('displayTeam')} cats={len(cats)}")
        for c in cats[:2]:
            evs = c.get("events", [])
            print(f"     cat '{c.get('displayName')}' type={c.get('type')} events={len(evs)}")
            for ev in evs[:3]:
                print(f"        eventId={ev.get('eventId')} stats={ev.get('stats')}")
    # what recent_combo_values returns
    print("  -> recent PRA:", w.recent_combo_values(info['player_id'], "Pts+Rebs+Asts", 8))
    print("  -> recent PR :", w.recent_combo_values(info['player_id'], "Pts+Rebs", 8))
