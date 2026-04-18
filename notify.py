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

_EMAIL_WRAP = """\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f4f6f8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f6f8;padding:32px 0;">
  <tr><td align="center">
    <table width="600" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,0.08);">
      <tr><td style="background:{header_color};padding:24px 32px;">
        <h1 style="margin:0;color:#fff;font-size:22px;font-weight:700;">{header_icon} {header_title}</h1>
        <p style="margin:6px 0 0;color:rgba(255,255,255,0.85);font-size:14px;">{header_sub}</p>
      </td></tr>
      <tr><td style="padding:28px 32px;">
        {body}
      </td></tr>
      <tr><td style="background:#f4f6f8;padding:16px 32px;border-top:1px solid #e8ecf0;">
        <p style="margin:0;color:#9aa5b4;font-size:12px;">PP Bug Scanner &bull; Runs every 15 min &bull; Reply to unsubscribe</p>
      </td></tr>
    </table>
  </td></tr>
</table>
</body></html>"""

_CARD = """\
<table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:12px;border:1px solid #e8ecf0;border-radius:8px;overflow:hidden;">
  <tr style="background:{accent};">
    <td style="padding:10px 16px;">
      <span style="color:#fff;font-weight:700;font-size:15px;">{player}</span>
      <span style="color:rgba(255,255,255,0.8);font-size:13px;margin-left:8px;">{league}</span>
    </td>
  </tr>
  <tr><td style="padding:14px 16px;">
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr>
        {cells}
      </tr>
    </table>
  </td></tr>
</table>"""

_CELL = '<td style="text-align:center;padding:0 12px 0 0;"><div style="color:#9aa5b4;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px;">{label}</div><div style="font-size:16px;font-weight:700;color:{color};">{value}</div></td>'


def _cell(label, value, color="#1a202c"):
    return _CELL.format(label=label, value=value, color=color)


def format_bugs_email(bugs: list) -> tuple[str, str, str]:
    count = len(bugs)
    subject = f"PrizePicks: {count} Bug{'s' if count > 1 else ''} Found"

    cards = ""
    plain_lines = []
    for b in bugs:
        gap = b.get("gap", 0)
        gap_str = f"+{gap} easier" if gap > 0 else "same line"
        moved = f"  (std moved {b['prev_std']} to {b['standard']})" if b.get("prev_std") else ""
        start = b.get("start_time", "")[:16].replace("T", " ")
        is_goblin = "goblin" in b.get("bug_type", "")
        line_label = "Goblin Line" if is_goblin else "Demon Line"
        line_color = "#e53e3e" if is_goblin else "#38a169"
        accent     = "#c05621" if is_goblin else "#e53e3e"
        cards += _CARD.format(
            accent=accent,
            player=b["player"],
            league=b["league"],
            cells=(
                _cell("Stat", b["stat"]) +
                _cell(line_label, b["bug_line"], line_color) +
                _cell("Standard", b["standard"], "#718096") +
                _cell("Edge", gap_str, line_color) +
                _cell("Game Time", start, "#4a5568")
            ),
        )
        plain_lines.append(
            f"  {b['player']} {b['stat']} [{b['league']}]: "
            f"{'goblin' if is_goblin else 'demon'}={b['bug_line']} std={b['standard']} ({gap_str}){moved} {start}"
        )

    if bugs:
        b0 = bugs[0]
        is_goblin0 = "goblin" in b0.get("bug_type", "")
        if is_goblin0:
            edge_desc = f"goblin {b0['bug_line']} is HARDER than standard {b0['standard']} — avoid this pick"
            tip = f"AVOID goblin {b0['bug_line']} — {edge_desc}"
        else:
            edge_desc = f"{b0['gap']} units easier than standard at demon payout" if b0.get("gap") else "same difficulty as standard pick but higher payout"
            tip = f"Bet DEMON OVER {b0['bug_line']} — {edge_desc}"
    else:
        tip = ""

    body = cards + f'<p style="margin:16px 0 0;padding:12px 16px;background:#f0fff4;border-left:4px solid #38a169;border-radius:4px;color:#276749;font-size:13px;">{tip}</p>'

    html = _EMAIL_WRAP.format(
        header_color="#e53e3e",
        header_icon="",
        header_title=f"{count} Exploitable Bug{'s' if count > 1 else ''} on PrizePicks",
        header_sub="A demon line is easier than the standard line — guaranteed edge",
        body=body,
    )
    plain = subject + "\n\n" + "\n".join(plain_lines) + f"\n\n{tip}"
    return subject, html, plain


def format_flash_email(sales: list) -> tuple[str, str, str]:
    count = len(sales)
    subject = f"PrizePicks: {count} Flash Sale{'s' if count > 1 else ''}"

    cards = ""
    plain_lines = []
    for s in sales:
        start = s.get("start_time", "")[:16].replace("T", " ")
        cards += _CARD.format(
            accent="#d97706",
            player=s["player"],
            league=s["league"],
            cells=(
                _cell("Stat", s["stat"]) +
                _cell("Normal Line", s["normal_line"], "#718096") +
                _cell("Sale Line", s["sale_line"], "#38a169") +
                _cell("Discount", f"-{s['discount']}", "#38a169") +
                _cell("Game Time", start, "#4a5568")
            ),
        )
        plain_lines.append(
            f"  {s['player']} {s['stat']}: {s['normal_line']} -> {s['sale_line']} (-{s['discount']}) | {start}"
        )

    html = _EMAIL_WRAP.format(
        header_color="#d97706",
        header_icon="",
        header_title=f"{count} Flash Sale{'s' if count > 1 else ''} — Act Fast",
        header_sub="Flash sales typically expire in 15–60 minutes",
        body=cards,
    )
    plain = subject + "\n\n" + "\n".join(plain_lines)
    return subject, html, plain


def format_promo_email(promos: list) -> tuple[str, str, str]:
    count = len(promos)
    subject = f"PrizePicks: {count} Promo Line{'s' if count > 1 else ''}"

    cards = ""
    plain_lines = []
    for p in promos:
        start = p.get("start_time", "")[:16].replace("T", " ")
        cards += _CARD.format(
            accent="#6b46c1",
            player=p["player"],
            league=p["league"],
            cells=(
                _cell("Stat", p["stat"]) +
                _cell("Type", p["odds_type"], "#6b46c1") +
                _cell("Line", p["line"], "#1a202c") +
                _cell("Game Time", start, "#4a5568")
            ),
        )
        plain_lines.append(
            f"  {p['player']} {p['stat']} [{p['league']}]: {p['odds_type']} {p['line']} (PROMO) | {start}"
        )

    html = _EMAIL_WRAP.format(
        header_color="#6b46c1",
        header_icon="",
        header_title=f"{count} Boosted Promo Line{'s' if count > 1 else ''}",
        header_sub="Cross-check vs sportsbook consensus for best picks",
        body=cards,
    )
    plain = subject + "\n\n" + "\n".join(plain_lines)
    return subject, html, plain


def format_consensus_email(edges: list[dict], correlated: list[dict]) -> tuple[str, str, str]:
    """Returns (subject, html, plain) for consensus edge alerts."""
    count   = len(edges)
    subject = f"Line Value: {count} PP Line{'s' if count != 1 else ''} Way Off Consensus"

    cards = ""
    plain_lines = []

    # Correlated parlays first
    for c in correlated:
        leg_text = ", ".join(f"{l['stat']} ({l['direction'].upper()} {l['platform_line']})" for l in c["legs"])
        cards += _CARD.format(
            accent="#1a56db",
            player=c["player"],
            league=f"CORRELATED {c['direction'].upper()} PARLAY",
            cells=(
                _cell("Legs", str(len(c["legs"])), "#1a56db") +
                _cell("Avg Edge", f"{c['avg_pct']}%", "#38a169") +
                _cell("Stats", leg_text, "#4a5568")
            ),
        )
        plain_lines.append(f"  PARLAY {c['direction'].upper()}: {c['player']} — {leg_text} (avg {c['avg_pct']}% off consensus)")

    # Individual edges — double-confirmed first
    for e in sorted(edges, key=lambda x: (not x.get("multiplier_confirmed", False), -x["abs_diff"])):
        arrow = "BET MORE/OVER" if e["direction"] == "over" else "BET LESS/UNDER"
        color = "#38a169" if e["direction"] == "over" else "#e53e3e"
        confirmed = e.get("multiplier_confirmed", False)
        accent = "#1a56db" if confirmed else color
        badge = " ★ MULT CONFIRMED" if confirmed else ""
        cards += _CARD.format(
            accent=accent,
            player=e["player"],
            league=e["league"] + badge,
            cells=(
                _cell("Stat", e["stat"]) +
                _cell(f"{e['platform'].upper()} Line", str(e["platform_line"]), color) +
                _cell("Consensus", str(e["consensus"]), "#718096") +
                _cell("Gap", f"{e['diff']:+.1f} ({e['pct_diff']}%)", color) +
                _cell("Play", arrow, color)
            ),
        )
        plain_lines.append(
            f"  {'★ ' if confirmed else ''}{e['player']} {e['stat']} [{e['league']}]: "
            f"line={e['platform_line']} consensus={e['consensus']} "
            f"diff={e['diff']:+.1f} ({e['pct_diff']}%) → {arrow}"
            + (" [MULT CONFIRMED]" if confirmed else "")
        )

    html = _EMAIL_WRAP.format(
        header_color="#1a56db",
        header_icon="",
        header_title=f"{count} Line{'s' if count != 1 else ''} Significantly Off Sportsbook Consensus",
        header_sub="Platform lines compared to DraftKings, FanDuel, BetMGM, Caesars and others",
        body=cards,
    )
    plain = subject + "\n\n" + "\n".join(plain_lines)
    return subject, html, plain


def format_ud_bugs_email(bugs: list) -> tuple[str, str, str]:
    count = len(bugs)
    subject = f"Underdog: {count} Mispriced Line{'s' if count > 1 else ''} Found"

    cards = ""
    plain_lines = []
    for b in bugs:
        bug_type = b.get("bug_type", "")
        if bug_type in ("easy_alternate", "mispriced_alternate"):
            cells = (
                _cell("Stat", b["stat"]) +
                _cell("Alt Line", b["alt_value"], "#38a169") +
                _cell("Balanced", b["balanced"], "#718096") +
                _cell("Multiplier", f"{b['alt_mult']:.2f}x", "#38a169") +
                _cell("Gap", f"+{b['gap']}", "#38a169")
            )
            plain_detail = f"alt={b['alt_value']} bal={b['balanced']} mult={b['alt_mult']:.3f} gap={b['gap']}"
        else:
            exp = str(b.get("expires_at", ""))[:16].replace("T", " ")
            cells = (
                _cell("Stat", b["stat"]) +
                _cell("Balanced", b["balanced"]) +
                _cell("Expires", exp, "#d97706")
            )
            plain_detail = f"bal={b['balanced']} expires={exp}"

        cards += _CARD.format(
            accent="#dd6b20",
            player=b["player"],
            league=b.get("sport", ""),
            cells=cells,
        )
        plain_lines.append(f"  [{b.get('sport','')}] {b['player']} {b['stat']}: {plain_detail}")

    html = _EMAIL_WRAP.format(
        header_color="#dd6b20",
        header_icon="",
        header_title=f"{count} Mispriced Underdog Line{'s' if count > 1 else ''}",
        header_sub="Easier than the balanced line at the same or better payout",
        body=cards,
    )
    plain = subject + "\n\n" + "\n".join(plain_lines)
    return subject, html, plain
