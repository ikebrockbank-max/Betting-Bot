"""
notify.py — unified notification sender (email + SMS).

Reads credentials from environment variables (set in .env or Railway dashboard):

  RESEND_API_KEY   — from resend.com (free: 3,000 emails/month)
  NOTIFY_EMAIL     — your email address to receive alerts
  NOTIFY_FROM      — sender address (e.g. alerts@yourdomain.com or onboarding@resend.dev for testing)

  TWILIO_ACCOUNT_SID  — from twilio.com console (optional)
  TWILIO_AUTH_TOKEN   — from twilio.com console (optional)
  TWILIO_FROM_NUMBER  — your Twilio phone number, e.g. +15551234567 (optional)
  NOTIFY_PHONE        — your cell number to receive SMS, e.g. +15559876543 (optional)
"""

import os
import smtplib
import requests
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv

load_dotenv()

RESEND_API_KEY   = os.getenv("RESEND_API_KEY", "")
NOTIFY_EMAIL     = os.getenv("NOTIFY_EMAIL", "")
NOTIFY_FROM      = os.getenv("NOTIFY_FROM", "onboarding@resend.dev")

TWILIO_SID       = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN     = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM      = os.getenv("TWILIO_FROM_NUMBER", "")
NOTIFY_PHONE     = os.getenv("NOTIFY_PHONE", "")

NOTIFY_PHONE_CARRIER_EMAIL = os.getenv("NOTIFY_PHONE_CARRIER_EMAIL", "")

# Gmail SMTP (free, works for any destination including carrier gateways)
GMAIL_USER         = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")


def _send_via_gmail(to: str, subject: str, body: str) -> bool:
    """Send email via Gmail SMTP. Works for any recipient including carrier gateways."""
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = GMAIL_USER
        msg["To"]      = to
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            smtp.sendmail(GMAIL_USER, to, msg.as_string())
        return True
    except Exception as e:
        print(f"[notify] Gmail send failed: {e}")
        return False


def send_email(subject: str, html_body: str, plain_body: str = "") -> bool:
    """Send an alert email. Uses Gmail if configured, falls back to Resend."""
    if not NOTIFY_EMAIL:
        return False
    # Gmail first (works without domain verification)
    if GMAIL_USER and GMAIL_APP_PASSWORD:
        return _send_via_gmail(NOTIFY_EMAIL, subject, plain_body or html_body)
    if not RESEND_API_KEY:
        return False

    payload = {
        "from":    NOTIFY_FROM,
        "to":      [NOTIFY_EMAIL],
        "subject": subject,
        "html":    html_body,
    }
    if plain_body:
        payload["text"] = plain_body

    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json=payload,
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[notify] Email failed: {e}")
        return False


def send_free_sms(message: str) -> bool:
    """Send a free SMS via carrier email gateway. Uses Gmail if configured, else Resend.

    Most US carriers provide a free email-to-SMS gateway, e.g.:
      AT&T:    number@txt.att.net
      Verizon: number@vtext.com
      T-Mobile: number@tmomail.net

    Set NOTIFY_PHONE_CARRIER_EMAIL to your gateway address.
    The email subject becomes the SMS header on most carriers.
    Skips silently if NOTIFY_PHONE_CARRIER_EMAIL is not set.
    """
    if not NOTIFY_PHONE_CARRIER_EMAIL:
        return False
    subject = message.replace("\n", " ")[:40]
    # Gmail is preferred — works for any carrier gateway without domain restrictions
    if GMAIL_USER and GMAIL_APP_PASSWORD:
        return _send_via_gmail(NOTIFY_PHONE_CARRIER_EMAIL, subject, message)
    # Resend fallback (requires verified domain for external recipients)
    if not RESEND_API_KEY:
        return False
    payload = {
        "from":    NOTIFY_FROM,
        "to":      [NOTIFY_PHONE_CARRIER_EMAIL],
        "subject": subject,
        "text":    message,
    }
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json=payload,
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[notify] Free SMS (carrier email) failed: {e}")
        return False


def send_sms(message: str) -> bool:
    """Send an SMS via Twilio, falling back to free carrier-email SMS if Twilio is not configured."""
    if all([TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM, NOTIFY_PHONE]):
        try:
            resp = requests.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
                auth=(TWILIO_SID, TWILIO_TOKEN),
                data={"From": TWILIO_FROM, "To": NOTIFY_PHONE, "Body": message},
                timeout=10,
            )
            resp.raise_for_status()
            return True
        except Exception as e:
            print(f"[notify] SMS failed: {e}")
            return False
    else:
        return send_free_sms(message)


def alert(subject: str, html: str, plain: str, sms_msg: str):
    """Send both email and SMS. Silently skips whichever isn't configured."""
    send_email(subject, html, plain)
    send_sms(sms_msg)


# ── Email formatters ──────────────────────────────────────────────────────────

def format_bugs_email(bugs: list) -> tuple[str, str, str]:
    """Returns (subject, html, plain) for a list of bug dicts."""
    count  = len(bugs)
    subject = f"🚨 {count} PrizePicks Bug{'s' if count > 1 else ''} Found!"

    rows = ""
    plain_lines = []
    for b in bugs:
        gap_str   = f"gap={b['gap']}" if b.get("gap", 0) > 0 else "SAME LINE"
        moved_str = (f" <em>(std moved {b['prev_std']}→{b['standard']})</em>"
                     if b.get("prev_std") else "")
        start     = b.get("start_time", "")[:16]
        rows += (
            f"<tr>"
            f"<td><b>{b['player']}</b></td>"
            f"<td>{b['stat']}</td>"
            f"<td>{b['league']}</td>"
            f"<td style='color:green'><b>demon {b['bug_line']}</b> vs std {b['standard']}</td>"
            f"<td>{gap_str}{moved_str}</td>"
            f"<td>{start}</td>"
            f"</tr>"
        )
        plain_lines.append(
            f"★ {b['player']} {b['stat']} [{b['league']}]: "
            f"demon={b['bug_line']} std={b['standard']} ({gap_str}) {start}"
        )

    html = f"""
<h2 style="color:#e74c3c">🚨 {count} Exploitable PrizePicks Bug{'s' if count > 1 else ''}</h2>
<table border="1" cellpadding="6" style="border-collapse:collapse;font-family:monospace">
  <tr style="background:#f0f0f0">
    <th>Player</th><th>Stat</th><th>League</th>
    <th>Line Bug</th><th>Gap</th><th>Game Start</th>
  </tr>
  {rows}
</table>
<p style="color:#888;font-size:12px">Bet demon OVER {bugs[0]['bug_line'] if bugs else '?'} —
{'same difficulty as standard but higher payout' if not bugs[0].get('gap') else f"easier than standard by {bugs[0]['gap']} units"}</p>
"""
    plain = subject + "\n\n" + "\n".join(plain_lines)
    return subject, html, plain


def format_flash_email(sales: list) -> tuple[str, str, str]:
    """Returns (subject, html, plain) for flash sale dicts."""
    count   = len(sales)
    subject = f"⚡ {count} PrizePicks Flash Sale{'s' if count > 1 else ''}!"

    rows = ""
    plain_lines = []
    for s in sales:
        start = s.get("start_time", "")[:16]
        rows += (
            f"<tr>"
            f"<td><b>{s['player']}</b></td>"
            f"<td>{s['stat']}</td>"
            f"<td>{s['league']}</td>"
            f"<td><s>{s['normal_line']}</s> → <b style='color:green'>{s['sale_line']}</b></td>"
            f"<td style='color:green'>−{s['discount']}</td>"
            f"<td>{start}</td>"
            f"</tr>"
        )
        plain_lines.append(
            f"⚡ {s['player']} {s['stat']}: {s['normal_line']} → {s['sale_line']} (−{s['discount']}) | {start}"
        )

    html = f"""
<h2 style="color:#f39c12">⚡ {count} Flash Sale{'s' if count > 1 else ''} — Limited Time!</h2>
<table border="1" cellpadding="6" style="border-collapse:collapse;font-family:monospace">
  <tr style="background:#f0f0f0">
    <th>Player</th><th>Stat</th><th>League</th>
    <th>Line (Sale)</th><th>Discount</th><th>Game Start</th>
  </tr>
  {rows}
</table>
<p style="color:#e67e22"><b>Act fast — flash sales typically expire in 15–60 minutes.</b></p>
"""
    plain = subject + "\n\n" + "\n".join(plain_lines)
    return subject, html, plain


def format_promo_email(promos: list) -> tuple[str, str, str]:
    """Returns (subject, html, plain) for promo line dicts."""
    count   = len(promos)
    subject = f"🎯 {count} PrizePicks Promo Line{'s' if count > 1 else ''}"

    rows = ""
    plain_lines = []
    for p in promos:
        start = p.get("start_time", "")[:16]
        rows += (
            f"<tr>"
            f"<td><b>{p['player']}</b></td>"
            f"<td>{p['stat']}</td>"
            f"<td>{p['league']}</td>"
            f"<td>{p['odds_type']}</td>"
            f"<td><b>{p['line']}</b></td>"
            f"<td>{start}</td>"
            f"</tr>"
        )
        plain_lines.append(
            f"🎯 {p['player']} {p['stat']} [{p['league']}]: {p['odds_type']} {p['line']} (PROMO) | {start}"
        )

    html = f"""
<h2 style="color:#8e44ad">🎯 {count} Boosted Promo Line{'s' if count > 1 else ''}</h2>
<table border="1" cellpadding="6" style="border-collapse:collapse;font-family:monospace">
  <tr style="background:#f0f0f0">
    <th>Player</th><th>Stat</th><th>League</th>
    <th>Type</th><th>Line</th><th>Game Start</th>
  </tr>
  {rows}
</table>
<p>These lines have boosted multipliers. Cross-check vs sportsbook consensus for best picks.</p>
"""
    plain = subject + "\n\n" + "\n".join(plain_lines)
    return subject, html, plain


def format_ud_bugs_email(bugs: list) -> tuple[str, str, str]:
    """Returns (subject, html, plain) for a list of Underdog bug dicts."""
    count = len(bugs)
    subject = f"Underdog {count} Bug{'s' if count > 1 else ''} Found!"

    rows = ""
    plain_lines = []
    for b in bugs:
        bug_type = b.get("bug_type", "")
        if bug_type == "easy_alternate":
            detail = (
                f"<td style='color:green'><b>alt {b['alt_value']}</b> vs bal {b['balanced']}</td>"
                f"<td>{b['alt_mult']:.3f}x</td>"
                f"<td style='color:green'>+{b['gap']}</td>"
            )
            plain_detail = (
                f"alt={b['alt_value']} bal={b['balanced']} "
                f"mult={b['alt_mult']:.3f} gap={b['gap']}"
            )
        else:  # expiring_line
            exp = str(b.get("expires_at", ""))[:19]
            detail = (
                f"<td><b>bal {b['balanced']}</b></td>"
                f"<td>—</td>"
                f"<td style='color:orange'>expires {exp}</td>"
            )
            plain_detail = f"bal={b['balanced']} expires={exp}"

        rows += (
            f"<tr>"
            f"<td><b>{b['player']}</b></td>"
            f"<td>{b['stat']}</td>"
            f"<td>{b['sport']}</td>"
            f"<td>{bug_type}</td>"
            f"{detail}"
            f"</tr>"
        )
        exp_str = str(b.get("expires_at", ""))[:16]
        plain_lines.append(
            f"[{b['sport']}] {b['player']} {b['stat']}: "
            f"{plain_detail}"
            + (f" | exp={exp_str}" if exp_str else "")
        )

    html = f"""
<h2 style="color:#e67e22">Underdog {count} Exploitable Bug{'s' if count > 1 else ''}</h2>
<table border="1" cellpadding="6" style="border-collapse:collapse;font-family:monospace">
  <tr style="background:#f0f0f0">
    <th>Player</th><th>Stat</th><th>Sport</th>
    <th>Bug Type</th><th>Line</th><th>Mult</th><th>Gap / Expiry</th>
  </tr>
  {rows}
</table>
<p style="color:#888;font-size:12px">
  easy_alternate: bet HIGHER on the alternate — easier than balanced at boosted payout.<br>
  expiring_line: balanced line expires soon — check for flash-sale edge.
</p>
"""
    plain = subject + "\n\n" + "\n".join(plain_lines)
    return subject, html, plain
