"""One-shot test — sends a test email and SMS to confirm notifications work."""
from notify import send_email, send_free_sms

ok_sms   = send_free_sms("PP Bot test - texts are working!")
ok_email = send_email("PP Bot test", "<p>Email alerts are working!</p>", "Email alerts are working!")
print("SMS:",   "sent" if ok_sms   else "FAILED")
print("Email:", "sent" if ok_email else "FAILED")
