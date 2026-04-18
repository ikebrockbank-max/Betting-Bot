"""One-shot test — sends a mock bug alert styled like a real find."""
from notify import send_push, send_email, format_bugs_email

mock_bug = {
    "player": "DeAnthony Melton",
    "stat": "Rebounds+Assists",
    "league": "NBA",
    "game_id": "test",
    "bug_line": 7.5,
    "standard": 9.5,
    "bug_type": "demon_easy",
    "gap": 2.0,
    "start_time": "2026-04-18T21:00:00",
}

subject, html, plain = format_bugs_email([mock_bug])
sms = f"PP Bug: {mock_bug['player']} {mock_bug['stat']} demon={mock_bug['bug_line']} vs std={mock_bug['standard']} (+{mock_bug['gap']})"

ok_push  = send_push(sms, title=subject[:50])
ok_email = send_email(subject, html, plain)
print("Push:",  "sent" if ok_push  else "FAILED")
print("Email:", "sent" if ok_email else "FAILED")
