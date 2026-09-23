"""Verify the atomic send-claim: does the sentinel row INSERT cleanly (no NOT
NULL failures), and does a duplicate return 409? Uses a throwaway test date."""
import json, urllib.request, urllib.error
from morning_digest import _SB_URL, _SB_KEY, _sb_headers

print("key present:", bool(_SB_KEY))
row = {"player": "__CLAIM_TEST__", "sport": "MARKER", "stat_type": "__test__",
       "pick_date": "2099-01-01", "line": 0.0, "direction": "",
       "confidence": 0, "resolved": True, "was_qualified": False}


def post():
    req = urllib.request.Request(f"{_SB_URL}/rest/v1/pick_log",
                                 data=json.dumps(row).encode(),
                                 headers={**_sb_headers(), "Prefer": "return=minimal"},
                                 method="POST")
    try:
        urllib.request.urlopen(req, timeout=10)
        return "OK-inserted"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}: {e.read().decode()[:220]}"


# clean any leftover first
def cleanup():
    p = ("pick_date=eq.2099-01-01&player=eq.__CLAIM_TEST__&stat_type=eq.__test__")
    req = urllib.request.Request(f"{_SB_URL}/rest/v1/pick_log?{p}",
                                 headers=_sb_headers(), method="DELETE")
    try:
        urllib.request.urlopen(req, timeout=10); return "OK"
    except Exception as e:
        return str(e)


cleanup()
print("insert #1 (expect OK-inserted):", post())
print("insert #2 (expect HTTP 409):   ", post())
print("cleanup:", cleanup())
