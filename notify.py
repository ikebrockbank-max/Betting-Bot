"""
notify.py — unified notification sender (email + push notification).

Reads credentials from environment variables (set in .env or GitHub secrets):

  GMAIL_USER         — your Gmail address
  GMAIL_APP_PASSWORD — Gmail app password (not your regular password)
  NOTIFY_EMAIL       — destination email address for alerts
  NTFY_TOPIC         — ntfy.sh topic name for push notifications (free, no account needed)
"""

import os
import smtplib
import requests
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv

load_dotenv()

NOTIFY_EMAIL     = os.getenv("NOTIFY_EMAIL", "")
GMAIL_USER         = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
NTFY_TOPIC         = os.getenv("NTFY_TOPIC", "")


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


def send_push(message: str, title: str = "PP Bot Alert") -> bool:
    """Send a push notification via ntfy.sh (free, no account needed).
    Install the ntfy app and subscribe to your NTFY_TOPIC to receive alerts.
    """
    if not NTFY_TOPIC:
        return False
    # HTTP headers must be ASCII — strip any non-ASCII characters from title
    safe_title = title.encode("ascii", errors="ignore").decode("ascii").strip()
    try:
        resp = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": safe_title, "Priority": "high", "Tags": "money_with_wings"},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[notify] Push notification failed: {e}")
        return False


def send_sms(message: str) -> bool:
    """Send a push notification (ntfy.sh). Named send_sms for backwards compatibility."""
    return send_push(message)


def alert(subject: str, html: str, plain: str, sms_msg: str):
    """Send email + push notification. Silently skips whichever isn't configured."""
    send_email(subject, html, plain)
    send_push(sms_msg, title=subject[:50])


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
        if bug_type in ("easy_alternate", "mispriced_alternate"):
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
