from __future__ import annotations

import html
import logging
import smtplib
from email.message import EmailMessage
from email.utils import parseaddr
from urllib.parse import urljoin


logger = logging.getLogger(__name__)


def build_reminder_message(app, lead, day_after_start: int) -> tuple[str, str, str]:
    name = (lead["full_name"] or "there").strip()
    short_code = app.config["USSD_SHORT_CODE"]
    support = app.config["SUPPORT_EMAIL"]
    base_url = app.config.get("PUBLIC_BASE_URL", "http://localhost:5000").rstrip("/")
    unsubscribe_url = urljoin(base_url + "/", f"unsubscribe/{lead['unsubscribe_token']}")
    brand = app.config["BRAND_NAME"]
    subject = f"Reminder to continue your {brand} sign-up"
    text = (
        f"Dear {name},\n\n"
        f"You started signing up with {brand} but haven't completed the process yet. "
        f"When you're ready, dial {short_code} and choose 1 to continue.\n\n"
        f"This is your {day_after_start}-day reminder. If you have already completed your sign-up, "
        f"you can ignore this email. For help, contact {support}.\n\n"
        f"To stop receiving sign-up reminders, visit: {unsubscribe_url}\n"
    )
    safe_name = html.escape(name)
    safe_code = html.escape(short_code)
    safe_brand = html.escape(brand)
    safe_support = html.escape(support)
    safe_unsubscribe = html.escape(unsubscribe_url, quote=True)
    html_body = (
        '<!doctype html><html><body style="font-family:Arial,sans-serif;color:#192c3a;line-height:1.6">'
        f"<p>Dear {safe_name},</p>"
        f"<p>You started signing up with {safe_brand} but haven't completed the process yet. "
        f"When you're ready, dial <strong>{safe_code}</strong> and choose 1 to continue.</p>"
        f"<p>This is your {day_after_start}-day reminder. If you have already completed your sign-up, "
        f"you can ignore this email. For help, contact <a href=\"mailto:{safe_support}\">{safe_support}</a>.</p>"
        f'<p><a href="{safe_unsubscribe}">Stop receiving sign-up reminders</a></p>'
        "</body></html>"
    )
    return subject, text, html_body


def deliver_reminder(app, lead, day_after_start: int) -> str:
    """Deliver through configured SMTP, or record a safe local preview in console mode."""
    subject, text, html_body = build_reminder_message(app, lead, day_after_start)
    backend = app.config["EMAIL_BACKEND"]
    if backend == "console":
        logger.info("Reminder preview ready (day=%s, lead_id=%s); configure SMTP to deliver email.", day_after_start, lead["id"])
        return "previewed"
    if backend != "smtp":
        raise RuntimeError("EMAIL_BACKEND must be 'console' or 'smtp'.")

    if not app.config["SMTP_HOST"]:
        raise RuntimeError("SMTP_HOST is required when EMAIL_BACKEND=smtp.")
    _sender_name, sender_address = parseaddr(app.config["MAIL_FROM"])
    if not sender_address:
        raise RuntimeError("MAIL_FROM must contain a valid sender email address.")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = app.config["MAIL_FROM"]
    message["To"] = lead["email"]
    message["Message-ID"] = f"<ekyc-signup-{lead['id']}-{day_after_start}@{sender_address.split('@')[-1]}>"
    message["List-Unsubscribe"] = f"<{app.config.get('PUBLIC_BASE_URL', 'http://localhost:5000').rstrip('/')}/unsubscribe/{lead['unsubscribe_token']}>"
    message.set_content(text)
    message.add_alternative(html_body, subtype="html")

    with smtplib.SMTP(app.config["SMTP_HOST"], app.config["SMTP_PORT"], timeout=20) as smtp:
        if app.config["SMTP_USE_TLS"]:
            smtp.starttls()
        if app.config["SMTP_USERNAME"]:
            smtp.login(app.config["SMTP_USERNAME"], app.config["SMTP_PASSWORD"])
        smtp.send_message(message)
    logger.info("Reminder email delivered (day=%s, lead_id=%s).", day_after_start, lead["id"])
    return "sent"

