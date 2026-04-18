"""One-shot test — sends a test push notification and email to confirm alerts work."""
from notify import send_email, send_push

ok_push  = send_push("PP Bot test - push notifications are working!")
ok_email = send_email("PP Bot test", "<p>Email alerts are working!</p>", "Email alerts are working!")
print("Push:",  "sent" if ok_push  else "FAILED")
print("Email:", "sent" if ok_email else "FAILED")
