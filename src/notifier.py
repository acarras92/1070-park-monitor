"""Email alerts via Gmail SMTP. Never raises — logs failures so state stays intact."""
from __future__ import annotations

import logging
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

from diff import Change

log = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


def _fmt_usd(v: int | None) -> str:
    if v is None:
        return "—"
    return f"${v:,}"


def _fmt_num(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _change_summary(c: Change) -> str:
    l = c.listing
    unit = l.unit or "?"
    beds = _fmt_num(l.bedrooms)
    baths = _fmt_num(l.bathrooms)
    if c.kind == "new":
        return f"NEW: {unit} — {_fmt_usd(l.price)} ({beds}BR/{baths}BA)"
    if c.kind == "price":
        delta = (c.new_price or 0) - (c.old_price or 0)
        pct = (delta / c.old_price * 100) if c.old_price else 0
        return (
            f"PRICE CHANGE: {unit} {_fmt_usd(c.old_price)} → {_fmt_usd(c.new_price)} "
            f"(Δ {_fmt_usd(delta) if delta >= 0 else '-' + _fmt_usd(abs(delta))}, {pct:+.1f}%)"
        )
    if c.kind == "status":
        return f"STATUS: {unit} {c.old_status} → {c.new_status}"
    return f"{c.kind}: {unit}"


def _subject(changes: list[Change]) -> str:
    if len(changes) == 1:
        return f"[1070 Park] {_change_summary(changes[0])}"
    kinds = sorted({c.kind for c in changes})
    tag = "/".join(k.upper() for k in kinds)
    return f"[1070 Park] {tag} — {len(changes)} changes"


def _row_html(c: Change) -> str:
    l = c.listing
    price_cell = _fmt_usd(l.price)
    if c.kind == "price":
        delta = (c.new_price or 0) - (c.old_price or 0)
        pct = (delta / c.old_price * 100) if c.old_price else 0
        price_cell = (
            f"<b>{_fmt_usd(c.old_price)} → {_fmt_usd(c.new_price)}</b>"
            f"<br><span style='color:{'green' if delta < 0 else 'red'}'>"
            f"{'+' if delta >= 0 else '-'}{_fmt_usd(abs(delta))} ({pct:+.1f}%)</span>"
        )
    status_cell = escape(l.status or "")
    if c.kind == "status":
        status_cell = f"<b>{escape(c.old_status or '—')} → {escape(c.new_status or '—')}</b>"

    img = (
        f"<img src='{escape(l.image_url)}' style='max-width:140px;max-height:100px'/>"
        if l.image_url
        else ""
    )
    return f"""
<tr>
  <td>{escape((l.unit or '?'))}</td>
  <td>{price_cell}</td>
  <td>{_fmt_num(l.bedrooms)}BR / {_fmt_num(l.bathrooms)}BA</td>
  <td>{_fmt_num(l.square_feet)} sqft</td>
  <td>{_fmt_usd(l.maintenance)}/mo</td>
  <td>{status_cell}</td>
  <td>{escape(l.source)}</td>
  <td><a href='{escape(l.listing_url)}'>view</a></td>
  <td>{img}</td>
</tr>
"""


def build_html(changes: list[Change], scraped_at: str) -> str:
    summary_rows = "".join(f"<li>{escape(_change_summary(c))}</li>" for c in changes)
    table_rows = "".join(_row_html(c) for c in changes)
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family: -apple-system, Segoe UI, Arial, sans-serif; color:#111;">
  <h2 style="margin:0 0 8px 0">1070 Park Avenue — {len(changes)} change(s)</h2>
  <p style="color:#666;margin:0 0 12px 0">Scraped at {escape(scraped_at)}</p>
  <ul>{summary_rows}</ul>
  <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-size:13px">
    <thead style="background:#f3f3f3">
      <tr>
        <th>Unit</th><th>Price</th><th>Beds/Baths</th><th>Size</th>
        <th>Maint</th><th>Status</th><th>Source</th><th>Link</th><th>Photo</th>
      </tr>
    </thead>
    <tbody>{table_rows}</tbody>
  </table>
</body>
</html>"""


def send(
    *,
    subject: str,
    html_body: str,
    smtp_user: str,
    smtp_password: str,
    to_addr: str,
    bcc_addr: str | None = None,
    high_priority: bool = True,
) -> bool:
    """Send an email. Returns True on success, False otherwise. Never raises."""
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = smtp_user
        msg["To"] = to_addr
        if high_priority:
            msg["X-Priority"] = "1"
            msg["Importance"] = "High"
            msg["X-MSMail-Priority"] = "High"

        msg.attach(MIMEText("This notification is HTML. View in a modern client.", "plain"))
        msg.attach(MIMEText(html_body, "html"))

        recipients = [to_addr]
        if bcc_addr and bcc_addr != to_addr:
            recipients.append(bcc_addr)

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.ehlo()
            s.starttls()
            s.ehlo()
            s.login(smtp_user, smtp_password)
            s.sendmail(smtp_user, recipients, msg.as_string())

        log.info("email sent: %s (to=%s, bcc=%s)", subject, to_addr, bcc_addr)
        return True
    except Exception as e:
        log.exception("email send failed: %s", e)
        return False


def send_change_alert(
    changes: list[Change],
    *,
    smtp_user: str,
    smtp_password: str,
    to_addr: str,
    bcc_addr: str | None = None,
) -> bool:
    if not changes:
        return True
    now_iso = datetime.now(timezone.utc).isoformat()
    subject = _subject(changes)
    html = build_html(changes, now_iso)
    return send(
        subject=subject,
        html_body=html,
        smtp_user=smtp_user,
        smtp_password=smtp_password,
        to_addr=to_addr,
        bcc_addr=bcc_addr,
        high_priority=True,
    )


def send_test(
    *,
    smtp_user: str,
    smtp_password: str,
    to_addr: str,
    bcc_addr: str | None = None,
    body_html: str = "<p>1070 Park monitor test — ignore.</p>",
) -> bool:
    return send(
        subject="[1070 Park] test — ignore",
        html_body=body_html,
        smtp_user=smtp_user,
        smtp_password=smtp_password,
        to_addr=to_addr,
        bcc_addr=bcc_addr,
        high_priority=False,
    )
