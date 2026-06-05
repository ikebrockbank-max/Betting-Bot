"""
notify.py — unified notification sender (email + push + Discord webhooks).

Reads credentials from environment variables (set in .env or GitHub secrets):

  GMAIL_USER              — your Gmail address
  GMAIL_APP_PASSWORD      — Gmail app password (not your regular password)
  NOTIFY_EMAIL            — destination email address for alerts
  NTFY_TOPIC              — ntfy.sh topic name for push notifications (free, no account needed)
  DISCORD_WEBHOOK_PREMIUM — Discord webhook URL for paid #premium-alerts channel (real-time)
  DISCORD_WEBHOOK_FREE    — Discord webhook URL for free #free-picks channel (delayed)
"""

import os
import smtplib
import requests
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv

load_dotenv()

NOTIFY_EMAIL            = os.getenv("NOTIFY_EMAIL", "")
GMAIL_USER              = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD      = os.getenv("GMAIL_APP_PASSWORD", "")
NTFY_TOPIC              = os.getenv("NTFY_TOPIC", "")
DISCORD_WEBHOOK_PREMIUM = os.getenv("DISCORD_WEBHOOK_PREMIUM", "")
DISCORD_WEBHOOK_FREE    = os.getenv("DISCORD_WEBHOOK_FREE", "")


def _send_via_gmail(to: str, subject: str, html_body: str, plain_body: str = "") -> bool:
    """Send a multipart HTML+plain email via Gmail SMTP."""
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = to
    if plain_body:
        msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    # Try port 465 (SMTP_SSL) first, fall back to port 587 (STARTTLS)
    # Railway and some cloud hosts block 465 but allow 587
    for attempt, use_ssl, port in [(1, True, 465), (2, False, 587)]:
        try:
            if use_ssl:
                with smtplib.SMTP_SSL("smtp.gmail.com", port) as smtp:
                    smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
                    smtp.sendmail(GMAIL_USER, to, msg.as_string())
            else:
                with smtplib.SMTP("smtp.gmail.com", port) as smtp:
                    smtp.ehlo()
                    smtp.starttls()
                    smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
                    smtp.sendmail(GMAIL_USER, to, msg.as_string())
            return True
        except Exception as e:
            print(f"[notify] Gmail port {port} failed: {e}")

    return False


def send_email(subject: str, html_body: str, plain_body: str = "") -> bool:
    """Send an alert email. Uses Gmail if configured, falls back to Resend."""
    if not NOTIFY_EMAIL:
        return False
    # Gmail first (works without domain verification)
    if GMAIL_USER and GMAIL_APP_PASSWORD:
        return _send_via_gmail(NOTIFY_EMAIL, subject, html_body, plain_body)
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


def send_discord(
    title: str,
    picks: list,          # list of dicts: {player, direction, line, stat_type, prob, sport}
    sport: str = "",
    tier: str = "premium", # "premium" = real-time paid channel, "free" = delayed free channel
    color: int = None,
) -> bool:
    """Post a picks embed to a Discord webhook channel.

    tier="premium"  → posts to DISCORD_WEBHOOK_PREMIUM (real-time, for paid subscribers)
    tier="free"     → posts to DISCORD_WEBHOOK_FREE    (delayed ~60 min, public/free channel)

    Discord embed colors (decimal): green=3066993, gold=15844367, red=15158332, blue=3447003
    """
    webhook_url = DISCORD_WEBHOOK_PREMIUM if tier == "premium" else DISCORD_WEBHOOK_FREE
    if not webhook_url:
        return False

    # Choose color by sport if not specified
    if color is None:
        color = {"MLB": 15844367, "NBA": 3447003, "WNBA": 15105570}.get(sport, 3066993)

    # Sport emoji
    emoji = {"MLB": "⚾", "NBA": "🏀", "WNBA": "🏀"}.get(sport, "🎯")

    # Build pick lines
    fields = []
    for p in picks:
        conf = int(p.get("prob", 0) * 100)
        bar = "🟢" if conf >= 75 else "🟡"
        direction = p.get("direction", "").upper()
        arrow = "📈" if direction == "OVER" else "📉"
        fields.append({
            "name": f"{arrow} {p['player']} {direction} {p['line']} {p['stat_type']}",
            "value": f"{bar} **{conf}% confidence** | {p.get('sport', sport)}",
            "inline": False,
        })

    # Footer differs by tier
    if tier == "premium":
        footer = "⚡ Real-time alert — premium members only"
    else:
        footer = "🔓 Free pick (60-min delay) · Upgrade for real-time alerts"

    payload = {
        "embeds": [{
            "title": f"{emoji} {title}",
            "color": color,
            "fields": fields[:25],  # Discord max 25 fields
            "footer": {"text": footer},
        }]
    }

    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[notify] Discord webhook ({tier}) failed: {e}")
        return False


def send_discord_simple(message: str, title: str = "PP Bot Alert", tier: str = "premium") -> bool:
    """Post a plain-text message to Discord (no embed formatting)."""
    webhook_url = DISCORD_WEBHOOK_PREMIUM if tier == "premium" else DISCORD_WEBHOOK_FREE
    if not webhook_url:
        return False
    try:
        resp = requests.post(webhook_url, json={"content": f"**{title}**\n{message}"}, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[notify] Discord webhook ({tier}) failed: {e}")
        return False


def send_sms(message: str) -> bool:
    """Send a push notification (ntfy.sh). Named send_sms for backwards compatibility."""
    return send_push(message)


def alert(subject: str, html: str, plain: str, sms_msg: str):
    """Send email + push notification. Silently skips whichever isn't configured."""
    send_email(subject, html, plain)
    send_push(sms_msg, title=subject[:50])


# ── Email formatters ──────────────────────────────────────────────────────────

# ── Simple / mobile-first templates (Gmail-safe, no divs, no flex) ────────────

_SIMPLE_WRAP = """\
<!DOCTYPE html><html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:16px;background:#f0f2f5;font-family:Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">
<table width="560" cellpadding="0" cellspacing="0" style="max-width:560px;">
  <tr>
    <td style="background:{header_color};padding:20px 24px;border-radius:8px 8px 0 0;">
      <p style="margin:0;color:#fff;font-size:20px;font-weight:700;">{header_title}</p>
      <p style="margin:4px 0 0;color:rgba(255,255,255,0.8);font-size:13px;">{header_sub}</p>
    </td>
  </tr>
  <tr>
    <td style="background:#ffffff;padding:20px 24px;">
      {body}
    </td>
  </tr>
  <tr>
    <td style="background:#e8ecf0;padding:12px 24px;border-radius:0 0 8px 8px;">
      <p style="margin:0;color:#718096;font-size:11px;">Kalshi Bot &bull; Automated scanner &bull; Reply to unsubscribe</p>
    </td>
  </tr>
</table>
</td></tr></table>
</body></html>"""

_SIMPLE_CARD = """\
<table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:16px;border-radius:8px;overflow:hidden;border:1px solid #d1d5db;">
  <tr>
    <td style="background:{accent};padding:12px 16px;">
      <p style="margin:0;color:#fff;font-size:22px;font-weight:800;">{action}</p>
      <p style="margin:2px 0 0;color:rgba(255,255,255,0.85);font-size:14px;">{subtitle}</p>
    </td>
  </tr>
  <tr>
    <td style="background:#ffffff;padding:16px;">
      {rows}
    </td>
  </tr>
</table>"""

def _simple_row(label: str, value: str, value_color: str = "#1a202c") -> str:
    """A single label/value row for use inside _SIMPLE_CARD rows."""
    return (
        f'<table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:8px;">'
        f'<tr>'
        f'<td style="font-size:12px;color:#718096;text-transform:uppercase;letter-spacing:0.4px;">{label}</td>'
        f'<td align="right" style="font-size:14px;font-weight:700;color:{value_color};">{value}</td>'
        f'</tr>'
        f'</table>'
    )


# ── Legacy templates (kept for existing formatters) ───────────────────────────

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


# ── Digest email (single combined email for all finds) ────────────────────────

_VERDICT_META = {
    # (verdict, confidence) → (badge_label, header_color, emoji)
    ("OVER",        "STRONG"): ("STRONG OVER",  "#276749", "🟢"),
    ("OVER",        "LEAN"):   ("LEAN OVER",    "#2f855a", "🟡"),
    ("UNDER",       "STRONG"): ("STRONG UNDER", "#276749", "🟢"),
    ("UNDER",       "LEAN"):   ("LEAN UNDER",   "#2f855a", "🟡"),
    ("LEAN_OVER",   "CAUTION"):("LEAN OVER ⚠",  "#b7791f", "🟠"),
    ("LEAN_UNDER",  "CAUTION"):("LEAN UNDER ⚠", "#b7791f", "🟠"),
    ("CONFLICTING", "SKIP"):   ("CONFLICTING",  "#c53030", "🔴"),
    ("AVOID",       "SKIP"):   ("AVOID",        "#718096", "⛔"),
}

_LAST5_BOX = (
    '<span style="display:inline-block;width:26px;height:26px;line-height:26px;'
    'text-align:center;border-radius:4px;font-size:12px;font-weight:700;'
    'margin-right:3px;background:{bg};color:#fff;">{val}</span>'
)


def _last5_html(vals: list, line: float | None) -> str:
    boxes = []
    for v in vals[:5]:
        if line is not None:
            bg = "#38a169" if v >= line else "#e53e3e"
        else:
            bg = "#718096"
        boxes.append(_LAST5_BOX.format(bg=bg, val=int(v) if v == int(v) else round(v, 1)))
    return "".join(boxes)


def _verdict_card_html(v: dict) -> str:
    verdict    = v["verdict"]
    confidence = v["confidence"]
    badge_label, hdr_color, emoji = _VERDICT_META.get(
        (verdict, confidence), ("SIGNAL", "#4a5568", "📊")
    )

    direction = verdict.replace("LEAN_", "")   # "OVER" or "UNDER" or "CONFLICTING" etc.
    bet_line  = v.get("bet_line")
    stats     = v.get("stats")
    injury    = v.get("injury")
    signals   = v.get("signals", [])
    conflict  = v.get("conflict_detail", "")
    platform  = ", ".join(sorted({s["src"].upper() for s in signals}))

    # ── Header ──────────────────────────────────────────────────────────────
    action_html = ""
    if bet_line is not None and direction in ("OVER", "UNDER"):
        action_html = (
            f'<p style="margin:0 0 6px;font-size:20px;font-weight:800;color:{hdr_color};">'
            f'BET {direction} {bet_line}</p>'
        )
    elif verdict in ("LEAN_OVER", "LEAN_UNDER"):
        dir_word = "OVER" if verdict == "LEAN_OVER" else "UNDER"
        action_html = (
            f'<p style="margin:0 0 6px;font-size:18px;font-weight:800;color:#b7791f;">'
            f'LEAN {dir_word} {bet_line or ""}</p>'
        )
    elif verdict == "CONFLICTING":
        action_html = (
            '<p style="margin:0 0 6px;font-size:18px;font-weight:800;color:#c53030;">'
            '⚠ SKIP — signals conflict</p>'
        )
    elif verdict == "AVOID":
        action_html = (
            '<p style="margin:0 0 6px;font-size:18px;font-weight:800;color:#718096;">'
            '⛔ AVOID — player injured</p>'
        )

    reason_html = (
        f'<p style="margin:0 0 12px;font-size:13px;color:#4a5568;">{v.get("reason","")}</p>'
    )

    # ── Injury banner ────────────────────────────────────────────────────────
    injury_html = ""
    if injury:
        inj_color = "#c53030" if injury.get("disqualified") else "#b7791f"
        injury_html = (
            f'<p style="margin:0 0 10px;padding:8px 12px;background:#fff5f5;'
            f'border-left:4px solid {inj_color};border-radius:4px;'
            f'font-size:12px;color:{inj_color};">'
            f'⚠ {injury["status"]}: {injury["headline"]}</p>'
        )

    # ── Stats row ────────────────────────────────────────────────────────────
    stats_html = ""
    if stats:
        season = stats.get("season_avg", "—")
        l10    = stats.get("l10_avg",    "—")
        l5     = stats.get("l5_avg",     "—")
        last5  = stats.get("last_5",     [])
        s_min  = stats.get("season_min", "—")
        l5_min = stats.get("l5_min",     "—")
        mflag  = stats.get("minutes_flag")

        min_arrow = ""
        if mflag == "elevated":
            pct = round(stats.get("min_change_pct", 0) * 100)
            min_arrow = f' <span style="color:#38a169;font-weight:700;">↑+{pct}%</span>'
        elif mflag == "reduced":
            pct = round(abs(stats.get("min_change_pct", 0)) * 100)
            min_arrow = f' <span style="color:#e53e3e;font-weight:700;">↓−{pct}%</span>'

        boxes_html = _last5_html(last5, bet_line)

        stats_html = f"""
<div style="background:#f7fafc;border-radius:6px;padding:10px 12px;margin-bottom:10px;font-size:12px;">
  <div style="display:flex;gap:20px;margin-bottom:6px;">
    <span><span style="color:#9aa5b4;">Season avg</span> <strong>{season}</strong></span>
    <span><span style="color:#9aa5b4;">L10</span> <strong>{l10}</strong></span>
    <span><span style="color:#9aa5b4;">L5</span> <strong>{l5}</strong></span>
    <span><span style="color:#9aa5b4;">Min (L5)</span> <strong>{l5_min}</strong>{min_arrow} <span style="color:#9aa5b4;">vs season {s_min}</span></span>
  </div>
  <div style="margin-top:4px;"><span style="color:#9aa5b4;margin-right:6px;">Last 5:</span>{boxes_html}</div>
</div>"""

    # ── Conflict detail ───────────────────────────────────────────────────────
    conflict_html = ""
    if conflict:
        lines = conflict.replace("\n", "<br>")
        conflict_html = (
            f'<div style="margin-bottom:10px;padding:8px 12px;background:#fffaf0;'
            f'border-left:4px solid #ed8936;border-radius:4px;font-size:12px;color:#744210;">'
            f'<strong>Conflicting signals:</strong><br>{lines}</div>'
        )

    # ── Signal list ───────────────────────────────────────────────────────────
    sig_items = "".join(
        f'<li style="margin-bottom:3px;">'
        f'<span style="color:{"#38a169" if s["direction"]=="over" else "#e53e3e"};font-weight:700;">'
        f'{"↑" if s["direction"]=="over" else "↓"}</span> '
        f'{s["display"]}</li>'
        for s in signals
    )
    sig_html = (
        f'<ul style="margin:0;padding-left:16px;font-size:12px;color:#4a5568;">{sig_items}</ul>'
    )

    game_time = v.get("game_time", "")
    game_time_html = (
        f'<span style="color:rgba(255,255,255,0.65);font-size:11px;margin-left:10px;">🕐 {game_time}</span>'
        if game_time else ""
    )
    league_str = v.get("league") or platform

    return f"""
<table width="100%" cellpadding="0" cellspacing="0"
       style="margin-bottom:16px;border:1px solid #e8ecf0;border-radius:10px;overflow:hidden;">
  <tr style="background:{hdr_color};">
    <td style="padding:10px 16px;">
      <span style="background:rgba(255,255,255,0.2);color:#fff;font-size:11px;
                   font-weight:700;padding:2px 8px;border-radius:12px;
                   letter-spacing:0.5px;text-transform:uppercase;">{emoji} {badge_label}</span>
      <span style="color:#fff;font-weight:700;font-size:15px;margin-left:12px;">{v["player"]}</span>
      <span style="color:rgba(255,255,255,0.75);font-size:12px;margin-left:6px;">{v["stat"]} &bull; {league_str}</span>
      {game_time_html}
    </td>
  </tr>
  <tr><td style="padding:14px 16px;">
    {action_html}
    {reason_html}
    {injury_html}
    {stats_html}
    {conflict_html}
    <div style="color:#9aa5b4;font-size:11px;text-transform:uppercase;
                letter-spacing:0.5px;margin-bottom:4px;">Signals</div>
    {sig_html}
  </td></tr>
</table>"""


def format_digest_email(verdicts: list[dict]) -> tuple[str, str, str]:
    """
    Single combined email for all new finds across all platforms.
    Sorted by confidence: actionable STRONG picks first, conflicts last.
    """
    count   = len(verdicts)
    strong  = [v for v in verdicts if v["confidence"] == "STRONG"]
    lean    = [v for v in verdicts if v["confidence"] in ("LEAN", "CAUTION")]
    skip    = [v for v in verdicts if v["confidence"] == "SKIP"]

    subject = f"Scanner: {count} new find{'s' if count != 1 else ''}"
    if strong:
        top = strong[0]
        dir_word = top["verdict"].replace("LEAN_", "")
        subject  = f"🟢 {top['player']} {top['stat']} — BET {dir_word} {top['bet_line'] or ''}"
        if count > 1:
            subject += f" (+{count-1} more)"

    body = ""
    if strong:
        body += '<p style="margin:0 0 12px;font-size:13px;font-weight:700;color:#276749;">STRONG PICKS</p>'
        body += "".join(_verdict_card_html(v) for v in strong)
    if lean:
        body += '<p style="margin:16px 0 12px;font-size:13px;font-weight:700;color:#b7791f;">LEAN / CAUTION</p>'
        body += "".join(_verdict_card_html(v) for v in lean)
    if skip:
        body += '<p style="margin:16px 0 12px;font-size:13px;font-weight:700;color:#c53030;">REVIEW NEEDED</p>'
        body += "".join(_verdict_card_html(v) for v in skip)

    html = _EMAIL_WRAP.format(
        header_color="#1a202c",
        header_icon="📡",
        header_title=f"{count} New Find{'s' if count != 1 else ''} — Multi-Platform Scan",
        header_sub="PP • ParlayPlay • Underdog • Sportsbook consensus — conflicts resolved",
        body=body,
    )

    # Plain text
    lines = ["SCANNER DIGEST\n"]
    for v in verdicts:
        dir_word = v["verdict"].replace("LEAN_", "")
        bet_str  = f" {v['bet_line']}" if v.get("bet_line") else ""
        lines.append(f"[{v['confidence']}] {v['player']} {v['stat']} — BET {dir_word}{bet_str}")
        lines.append(f"  {v['reason']}")
        if v.get("conflict_detail"):
            lines.append(f"  CONFLICT: {v['conflict_detail'][:120]}...")
        s = v.get("stats")
        if s:
            lines.append(f"  Stats: season={s['season_avg']} L10={s['l10_avg']} L5={s['l5_avg']} last5={s.get('last_5',[])[:5]}")
        if v.get("injury"):
            lines.append(f"  INJURY: {v['injury']['status']} — {v['injury']['headline']}")
        lines.append("")

    return subject, html, "\n".join(lines)


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


def format_parlayplay_email(finds: list[dict]) -> tuple[str, str, str]:
    """Email formatter for ParlayPlay bugs and edges."""
    count = len(finds)
    subject = f"ParlayPlay: {count} Bug{'s' if count > 1 else ''} / Edge{'s' if count > 1 else ''} Found"

    cards = ""
    plain_lines = []

    for b in finds:
        bug_type = b.get("bug_type", "")

        if "cross" in bug_type:
            gap = b["gap"]
            color   = "#e53e3e" if gap > 0 else "#38a169"
            action  = "BET LESS on ParlayPlay" if gap > 0 else "BET MORE on ParlayPlay"
            cards += _CARD.format(
                accent="#1a56db",
                player=b["player"],
                league="ParlayPlay vs PrizePicks",
                cells=(
                    _cell("Stat",           b["stat"]) +
                    _cell("PrizePicks Line", str(b["pp_line"]),         "#718096") +
                    _cell("ParlayPlay Line", str(b["parlayplay_line"]), color) +
                    _cell("Gap",            f"{gap:+.1f} ({b['pct_gap']}%)", color) +
                    _cell("Play",           action, color)
                ),
            )
            plain_lines.append(
                f"  CROSS: {b['player']} {b['stat']}: PP={b['pp_line']} vs PLP={b['parlayplay_line']} "
                f"gap={gap:+.1f} ({b['pct_gap']}%) → {b['action']}"
            )

        elif "reversal" in bug_type:
            direction = b.get("direction", "")
            cards += _CARD.format(
                accent="#d97706",
                player=b["player"],
                league="ParlayPlay Mispriced Multiplier",
                cells=(
                    _cell("Stat",       b["stat"]) +
                    _cell("Direction",  direction, "#d97706") +
                    _cell("Low Line",   str(b["line_low"])) +
                    _cell("High Line",  str(b["line_high"])) +
                    _cell("Reversal",   f"{b['mult_low']}x→{b['mult_high']}x", "#e53e3e")
                ),
            )
            plain_lines.append(
                f"  MONO: {b['player']} {b['stat']}: {direction} mult reverses "
                f"{b['line_low']}→{b['line_high']} ({b['mult_low']}→{b['mult_high']}) → {b['action']}"
            )

        else:  # promo bug
            cards += _CARD.format(
                accent="#38a169",
                player=b["player"],
                league="ParlayPlay 🔥 Promo Edge",
                cells=(
                    _cell("Stat",        b["stat"]) +
                    _cell("Promo Line",  str(b.get("promo_line", "")),  "#38a169") +
                    _cell("Promo Mult",  f"{b.get('promo_mult', '')}x", "#38a169") +
                    _cell("Hard Line",   str(b.get("hard_line", b.get("easy_line", "")))) +
                    _cell("Edge",        f"+{b['edge']}x", "#38a169")
                ),
            )
            plain_lines.append(f"  PROMO: {b['player']} {b['stat']}: {b['action']}")

    html = _EMAIL_WRAP.format(
        header_color="#1a56db",
        header_icon="🎰",
        header_title=f"{count} ParlayPlay Bug{'s' if count > 1 else ''} / Edge{'s' if count > 1 else ''}",
        header_sub="Mispriced multipliers and cross-platform line gaps detected",
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


def format_arb_alert_email(arbs: list[dict]) -> tuple[str, str, str]:
    """
    Email formatter for multi-platform prediction market arb opportunities.
    Supports Kalshi × Polymarket and Kalshi × PredictIt.
    Profitable arbs shown first in green, watch list in yellow.
    """
    profitable = [a for a in arbs if a.get("profitable")]
    watching   = [a for a in arbs if not a.get("profitable")]
    count      = len(arbs)

    # Counterparties present (for subject line)
    cps = sorted({a.get("counterparty", "counter").capitalize() for a in arbs})
    cp_str = " × ".join(cps) if cps else "Multi-Platform"

    subject = f"🎯 {count} Prediction Market Arb{'s' if count != 1 else ''} — Kalshi × {cp_str}"
    if profitable:
        top = profitable[0]
        cp_label = top.get("counterparty", "counter").capitalize()
        subject = (
            f"✅ ARB [Kalshi×{cp_label}]: {top['kalshi_title'][:35]} | "
            f"{top['raw_edge_pct']} raw / {top['fee_adj_pct']} net"
        )
        if count > 1:
            subject += f" (+{count-1} more)"

    _FEE_NOTES = {
        "polymarket": "Polymarket ~2% on winning side",
        "predictit":  "PredictIt 10% of profit + 5% withdrawal — need 15%+ raw edge",
        "robinhood":  "Robinhood ~1% on winning side",
    }

    def _arb_card(a: dict, is_profitable: bool) -> str:
        accent      = "#276749" if is_profitable else "#b7791f"
        badge_color = "#f0fff4" if is_profitable else "#fffaf0"
        badge_text  = "✅ PROFITABLE" if is_profitable else "👀 WATCH — BELOW FEE THRESHOLD"
        badge_fg    = "#276749" if is_profitable else "#b7791f"

        cp       = a.get("counterparty", "counter")
        cp_label = cp.capitalize()

        arb_type = a.get("arb_type", "")
        if "YES_kalshi" in arb_type:
            buy1 = f"BUY YES on Kalshi @ {a.get('k_yes_ask_pct', '')}"
            buy2 = f"BUY NO on {cp_label} @ {a.get('p_no_price_pct', '')}"
        else:
            buy1 = f"BUY NO on Kalshi @ {a.get('k_no_ask_pct', '')}"
            buy2 = f"BUY YES on {cp_label} @ {a.get('p_yes_price_pct', '')}"

        close = a.get("close_time", "")[:10]
        vol_k = f"${int(a.get('kalshi_volume', 0)):,}"
        vol_p = f"${int(a.get('poly_volume',  0)):,}"

        # Platform link labels
        cp_url   = a.get("poly_url", "#")
        cp_domain = {
            "polymarket": "polymarket.com",
            "predictit":  "predictit.org",
            "robinhood":  "robinhood.com",
        }.get(cp, cp)

        # PredictIt position cap warning
        pi_cap_html = (
            '<p style="margin:4px 0 0;padding:4px 8px;background:#fefcbf;'
            'border-left:3px solid #d69e2e;border-radius:4px;font-size:11px;color:#744210;">'
            '⚠ PredictIt caps positions at $850/contract. Max profit per arb is limited.'
            '</p>'
        ) if cp == "predictit" else ""

        warning_html = (
            '<p style="margin:8px 0 0;padding:6px 10px;background:#fff5f5;'
            'border-left:3px solid #e53e3e;border-radius:4px;font-size:11px;color:#c53030;">'
            '⚠ Always verify both markets resolve on the exact same event/criteria before trading.'
            '</p>'
        ) if is_profitable else ""

        close_html = (
            f'<p style="margin:8px 0 0;font-size:11px;color:#9aa5b4;">📅 Resolves: {close}</p>'
            if close else ""
        )

        # Platform badge
        cp_badge_colors = {
            "polymarket": "#6c63ff",
            "predictit":  "#2b6cb0",
            "robinhood":  "#00c805",
        }
        cp_color = cp_badge_colors.get(cp, "#718096")

        return f"""
<table width="100%" cellpadding="0" cellspacing="0"
       style="margin-bottom:16px;border:1px solid #e8ecf0;border-radius:10px;overflow:hidden;">
  <tr style="background:{accent};">
    <td style="padding:10px 16px;">
      <span style="background:rgba(255,255,255,0.2);color:#fff;font-size:11px;
                   font-weight:700;padding:2px 8px;border-radius:12px;
                   letter-spacing:0.5px;text-transform:uppercase;">KALSHI × {cp_label.upper()}</span>
      <span style="color:#fff;font-weight:700;font-size:14px;margin-left:10px;">
        {a.get('kalshi_title','')[:60]}
      </span>
    </td>
  </tr>
  <tr><td style="padding:14px 16px;">
    <div style="display:inline-block;padding:4px 10px;background:{badge_color};
                border:1px solid {badge_fg};border-radius:6px;font-size:12px;
                font-weight:700;color:{badge_fg};margin-bottom:10px;">
      {badge_text}
    </div>
    <table width="100%" cellpadding="0" cellspacing="0"
           style="background:#f7fafc;border-radius:6px;margin-bottom:10px;">
      <tr>
        <td style="text-align:center;padding:10px;">
          <div style="color:#9aa5b4;font-size:10px;text-transform:uppercase;margin-bottom:3px;">Raw Edge</div>
          <div style="font-size:22px;font-weight:800;color:{accent};">{a.get('raw_edge_pct','')}</div>
        </td>
        <td style="text-align:center;padding:10px;border-left:1px solid #e8ecf0;">
          <div style="color:#9aa5b4;font-size:10px;text-transform:uppercase;margin-bottom:3px;">After Fees</div>
          <div style="font-size:22px;font-weight:800;color:{accent};">{a.get('fee_adj_pct','')}</div>
        </td>
        <td style="text-align:center;padding:10px;border-left:1px solid #e8ecf0;">
          <div style="color:#9aa5b4;font-size:10px;text-transform:uppercase;margin-bottom:3px;">Total Cost</div>
          <div style="font-size:22px;font-weight:800;color:#1a202c;">{a.get('total_cost', 0):.2%}</div>
        </td>
        <td style="text-align:center;padding:10px;border-left:1px solid #e8ecf0;">
          <div style="color:#9aa5b4;font-size:10px;text-transform:uppercase;margin-bottom:3px;">Match Score</div>
          <div style="font-size:22px;font-weight:800;color:#4a5568;">{a.get('match_score',0):.0%}</div>
        </td>
      </tr>
    </table>
    <p style="margin:0 0 6px;font-size:13px;font-weight:700;color:#1a202c;">How to trade:</p>
    <p style="margin:0 0 4px;padding:8px 12px;background:#edf2f7;border-radius:6px;
              font-size:13px;color:#2d3748;font-family:monospace;">
      1. {buy1}<br>2. {buy2}
    </p>
    {pi_cap_html}
    <table width="100%" cellpadding="0" cellspacing="0" style="font-size:12px;margin-top:10px;">
      <tr>
        <td width="49%" style="padding:8px;background:#f7fafc;border-radius:6px;vertical-align:top;">
          <div style="color:#9aa5b4;font-size:10px;text-transform:uppercase;margin-bottom:4px;">Kalshi</div>
          <div style="color:#1a202c;font-weight:600;margin-bottom:4px;">{a.get('kalshi_title','')[:55]}</div>
          <a href="{a.get('kalshi_url','#')}" style="color:#3182ce;">kalshi.com ↗</a>
          <div style="color:#718096;margin-top:4px;">Vol: {vol_k}</div>
        </td>
        <td width="2%"></td>
        <td width="49%" style="padding:8px;background:#f7fafc;border-radius:6px;vertical-align:top;">
          <div style="color:{cp_color};font-size:10px;text-transform:uppercase;
                      font-weight:700;margin-bottom:4px;">{cp_label}</div>
          <div style="color:#1a202c;font-weight:600;margin-bottom:4px;">{a.get('poly_question','')[:55]}</div>
          <a href="{cp_url}" style="color:#3182ce;">{cp_domain} ↗</a>
          <div style="color:#718096;margin-top:4px;">Vol: {vol_p}</div>
        </td>
      </tr>
    </table>
    {close_html}
    {warning_html}
  </td></tr>
</table>"""

    body = ""
    if profitable:
        body += '<p style="margin:0 0 12px;font-size:13px;font-weight:700;color:#276749;">✅ PROFITABLE — Act fast, prices move!</p>'
        body += "".join(_arb_card(a, True) for a in profitable)
    if watching:
        body += '<p style="margin:16px 0 12px;font-size:13px;font-weight:700;color:#b7791f;">👀 WATCH LIST — edge exists but below combined fee threshold</p>'
        body += "".join(_arb_card(a, False) for a in watching)

    # Fee footnote — list all platforms found
    fee_lines = "Kalshi ~7% of profit on winning side"
    for a in arbs:
        cp = a.get("counterparty", "")
        if cp in _FEE_NOTES:
            fee_lines += f" | {_FEE_NOTES[cp]}"
            break  # one note per platform is enough

    body += (
        '<div style="margin-top:16px;padding:10px 14px;background:#ebf8ff;'
        'border-left:4px solid #3182ce;border-radius:4px;font-size:12px;color:#2c5282;">'
        f'<strong>Fee structure:</strong> {fee_lines}.'
        '</div>'
    )

    html = _EMAIL_WRAP.format(
        header_color="#1a202c",
        header_icon="🎯",
        header_title=f"{count} Prediction Market Arb{'s' if count != 1 else ''} — Kalshi × {cp_str}",
        header_sub="True arbitrage: guaranteed profit regardless of outcome",
        body=body,
    )

    lines = ["PREDICTION MARKET ARB ALERT\n", f"{count} opportunity/ies\n"]
    for a in arbs:
        flag = "PROFITABLE" if a.get("profitable") else "WATCH"
        cp_label = a.get("counterparty", "counter").upper()
        lines.append(f"[{flag}][{cp_label}] {a.get('kalshi_title','')}")
        lines.append(f"  {a.get('action','')}")
        lines.append(f"  Match: {a.get('match_score',0):.0%} | Closes: {a.get('close_time','')[:10]}")
        lines.append(f"  Kalshi: {a.get('kalshi_url','')} | Counter: {a.get('poly_url','')}")
        lines.append("")

    return subject, html, "\n".join(lines)


def format_internal_arb_email(arbs: list[dict]) -> tuple[str, str, str]:
    """
    Email formatter for Kalshi internal arb opportunities (sweep + ordinal inversions).
    """
    sweeps   = [a for a in arbs if a["arb_type"] == "sweep"]
    ordinals = [a for a in arbs if a["arb_type"] == "ordinal_inversion"]
    count    = len(arbs)

    subject = f"🎯 {count} Kalshi Internal Arb{'s' if count != 1 else ''}"
    if sweeps:
        top = sweeps[0]
        subject = (
            f"✅ Kalshi Sweep Arb: {top['event_ticker']} | "
            f"{top['market_count']} markets @ {top['total_cost_cents']:.0f}¢ → {top['raw_edge_pct']} edge"
        )
        if count > 1:
            subject += f" (+{count-1} more)"

    def _sweep_card(a: dict) -> str:
        rows = ""
        for m in a["markets"][:8]:
            rows += (
                f'<tr>'
                f'<td style="padding:5px 8px;font-size:12px;color:#1a202c;">{m["title"][:65]}</td>'
                f'<td style="padding:5px 8px;text-align:right;font-size:13px;font-weight:700;'
                f'color:#276749;">{m["yes_ask"]:.1f}¢</td>'
                f'<td style="padding:5px 8px;text-align:right;font-size:12px;color:#718096;">'
                f'<a href="{m.get("url","#")}" style="color:#3182ce;">↗</a></td>'
                f'</tr>'
            )
        return f"""
<table width="100%" cellpadding="0" cellspacing="0"
       style="margin-bottom:16px;border:1px solid #c6f6d5;border-radius:10px;overflow:hidden;">
  <tr style="background:#276749;">
    <td style="padding:10px 16px;">
      <span style="background:rgba(255,255,255,0.2);color:#fff;font-size:11px;font-weight:700;
                   padding:2px 8px;border-radius:12px;letter-spacing:0.5px;text-transform:uppercase;">
        SWEEP ARB
      </span>
      <span style="color:#fff;font-weight:700;font-size:14px;margin-left:10px;">
        {a['event_ticker']}
      </span>
    </td>
  </tr>
  <tr><td style="padding:14px 16px;">
    <table width="100%" cellpadding="0" cellspacing="0"
           style="background:#f7fafc;border-radius:6px;margin-bottom:12px;">
      <tr>
        <td style="text-align:center;padding:10px;">
          <div style="color:#9aa5b4;font-size:10px;text-transform:uppercase;margin-bottom:3px;">Total Cost</div>
          <div style="font-size:22px;font-weight:800;color:#c53030;">{a['total_cost_cents']:.1f}¢</div>
        </td>
        <td style="text-align:center;padding:10px;border-left:1px solid #e8ecf0;">
          <div style="color:#9aa5b4;font-size:10px;text-transform:uppercase;margin-bottom:3px;">Pays Out</div>
          <div style="font-size:22px;font-weight:800;color:#276749;">100¢</div>
        </td>
        <td style="text-align:center;padding:10px;border-left:1px solid #e8ecf0;">
          <div style="color:#9aa5b4;font-size:10px;text-transform:uppercase;margin-bottom:3px;">Min Net Profit</div>
          <div style="font-size:22px;font-weight:800;color:#276749;">{a['worst_net_pct']}</div>
        </td>
        <td style="text-align:center;padding:10px;border-left:1px solid #e8ecf0;">
          <div style="color:#9aa5b4;font-size:10px;text-transform:uppercase;margin-bottom:3px;">Markets</div>
          <div style="font-size:22px;font-weight:800;color:#4a5568;">{a['market_count']}</div>
        </td>
      </tr>
    </table>
    <p style="margin:0 0 6px;font-size:13px;font-weight:700;color:#1a202c;">
      Buy YES on ALL {a['market_count']} outcomes — exactly one pays $1.00:
    </p>
    <table width="100%" cellpadding="0" cellspacing="0"
           style="border:1px solid #e8ecf0;border-radius:6px;overflow:hidden;">
      <tr style="background:#f0fff4;">
        <th style="padding:6px 8px;text-align:left;font-size:11px;color:#718096;
                   text-transform:uppercase;font-weight:600;">Market</th>
        <th style="padding:6px 8px;text-align:right;font-size:11px;color:#718096;
                   text-transform:uppercase;font-weight:600;">YES Ask</th>
        <th style="padding:6px 8px;width:30px;"></th>
      </tr>
      {rows}
      <tr style="background:#f0fff4;border-top:2px solid #c6f6d5;">
        <td style="padding:6px 8px;font-size:12px;font-weight:700;color:#1a202c;">TOTAL</td>
        <td style="padding:6px 8px;text-align:right;font-size:13px;font-weight:800;color:#c53030;">
          {a['total_cost_cents']:.1f}¢
        </td>
        <td></td>
      </tr>
    </table>
    <p style="margin:10px 0 0;padding:6px 10px;background:#fff5f5;border-left:3px solid #e53e3e;
              border-radius:4px;font-size:11px;color:#c53030;">
      ⚠ Only works if these markets are MUTUALLY EXCLUSIVE — verify that exactly one can resolve YES.
    </p>
  </td></tr>
</table>"""

    def _ordinal_card(a: dict) -> str:
        guar_color = "#276749" if a["guaranteed"] else "#b7791f"
        guar_text  = "✅ GUARANTEED PROFIT" if a["guaranteed"] else "⚠ MISPRICING (not fully risk-free)"
        guar_bg    = "#f0fff4" if a["guaranteed"] else "#fffaf0"

        return f"""
<table width="100%" cellpadding="0" cellspacing="0"
       style="margin-bottom:16px;border:1px solid #bee3f8;border-radius:10px;overflow:hidden;">
  <tr style="background:#2b6cb0;">
    <td style="padding:10px 16px;">
      <span style="background:rgba(255,255,255,0.2);color:#fff;font-size:11px;font-weight:700;
                   padding:2px 8px;border-radius:12px;letter-spacing:0.5px;text-transform:uppercase;">
        ORDINAL INVERSION
      </span>
      <span style="color:#fff;font-weight:700;font-size:14px;margin-left:10px;">
        {a['event_ticker']}
      </span>
    </td>
  </tr>
  <tr><td style="padding:14px 16px;">
    <div style="display:inline-block;padding:4px 10px;background:{guar_bg};
                border:1px solid {guar_color};border-radius:6px;font-size:12px;
                font-weight:700;color:{guar_color};margin-bottom:10px;">
      {guar_text}
    </div>
    <table width="100%" cellpadding="0" cellspacing="0"
           style="background:#f7fafc;border-radius:6px;margin-bottom:10px;">
      <tr>
        <td style="text-align:center;padding:10px;">
          <div style="color:#9aa5b4;font-size:10px;text-transform:uppercase;margin-bottom:3px;">Inversion Size</div>
          <div style="font-size:22px;font-weight:800;color:#2b6cb0;">{a['raw_edge_pct']}</div>
        </td>
        <td style="text-align:center;padding:10px;border-left:1px solid #e8ecf0;">
          <div style="color:#9aa5b4;font-size:10px;text-transform:uppercase;margin-bottom:3px;">Total Cost</div>
          <div style="font-size:22px;font-weight:800;color:#1a202c;">{a['total_cost']:.0%}</div>
        </td>
        <td style="text-align:center;padding:10px;border-left:1px solid #e8ecf0;">
          <div style="color:#9aa5b4;font-size:10px;text-transform:uppercase;margin-bottom:3px;">Min Net</div>
          <div style="font-size:22px;font-weight:800;color:{guar_color};">{a['min_net']:.1%}</div>
        </td>
      </tr>
    </table>
    <p style="margin:0 0 6px;font-size:13px;font-weight:700;color:#1a202c;">The pricing violation:</p>
    <table width="100%" cellpadding="0" cellspacing="0"
           style="border:1px solid #e8ecf0;border-radius:6px;overflow:hidden;margin-bottom:10px;">
      <tr style="background:#ebf8ff;">
        <td style="padding:8px 12px;font-size:12px;color:#9aa5b4;text-transform:uppercase;
                   font-weight:600;width:80px;">EASIER</td>
        <td style="padding:8px 12px;font-size:12px;color:#1a202c;">
          {a['easy_market']['title'][:65]}
        </td>
        <td style="padding:8px 12px;text-align:right;font-size:14px;font-weight:700;color:#276749;">
          {a['ya_easy']:.0%} YES
        </td>
      </tr>
      <tr>
        <td style="padding:8px 12px;font-size:12px;color:#9aa5b4;text-transform:uppercase;
                   font-weight:600;">HARDER</td>
        <td style="padding:8px 12px;font-size:12px;color:#1a202c;">
          {a['hard_market']['title'][:65]}
        </td>
        <td style="padding:8px 12px;text-align:right;font-size:14px;font-weight:700;color:#c53030;">
          {a['ya_hard']:.0%} YES ← overpriced
        </td>
      </tr>
    </table>
    <p style="margin:0 0 6px;font-size:12px;color:#718096;">
      Scenarios → above hard: {a['net_above_hard']:.1%} &nbsp;|&nbsp;
      between: {a['net_between']:.1%} &nbsp;|&nbsp;
      below easy: {a['net_below_easy']:.1%}
    </p>
    <p style="margin:6px 0 0;padding:6px 10px;background:#edf2f7;border-radius:4px;
              font-size:12px;color:#2d3748;font-family:monospace;">
      {a['action']}
    </p>
  </td></tr>
</table>"""

    body = ""
    if sweeps:
        body += '<p style="margin:0 0 12px;font-size:13px;font-weight:700;color:#276749;">✅ SWEEP ARBS — buy all outcomes, one must pay $1.00</p>'
        body += "".join(_sweep_card(a) for a in sweeps)
    if ordinals:
        body += '<p style="margin:16px 0 12px;font-size:13px;font-weight:700;color:#2b6cb0;">📐 ORDINAL INVERSIONS — harder outcome priced above easier</p>'
        body += "".join(_ordinal_card(a) for a in ordinals)

    body += (
        '<div style="margin-top:16px;padding:10px 14px;background:#ebf8ff;'
        'border-left:4px solid #3182ce;border-radius:4px;font-size:12px;color:#2c5282;">'
        '<strong>Internal arb note:</strong> These opportunities exist entirely within Kalshi — '
        'no second account needed. Sweep arbs require the outcomes to be mutually exclusive. '
        'Kalshi charges ~7% of profit on the winning contract.'
        '</div>'
    )

    html = _EMAIL_WRAP.format(
        header_color="#1a202c",
        header_icon="🎯",
        header_title=f"{count} Kalshi Internal Arb{'s' if count != 1 else ''}",
        header_sub="Guaranteed profit within Kalshi alone",
        body=body,
    )

    lines = ["KALSHI INTERNAL ARB ALERT\n", f"{count} opportunity/ies\n"]
    for a in arbs:
        if a["arb_type"] == "sweep":
            lines.append(f"[SWEEP] {a['event_ticker']}  cost={a['total_cost_cents']:.1f}¢  edge={a['raw_edge_pct']}")
            lines.append(f"  {a['action']}")
        else:
            guar = "GUARANTEED" if a["guaranteed"] else "NOT GUARANTEED"
            lines.append(f"[ORDINAL/{guar}] {a['event_ticker']}  inversion={a['raw_edge_pct']}")
            lines.append(f"  {a['action']}")
        lines.append("")

    return subject, html, "\n".join(lines)
