from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime, timedelta

from .db import get_db


EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _normalise_phone(phone: str) -> str:
    compact = re.sub(r"[\s().-]", "", phone)
    if compact.startswith("00"):
        compact = "+" + compact[2:]
    has_country_prefix = compact.startswith("+")
    digits = compact[1:] if has_country_prefix else compact
    if not digits.isdigit() or not 7 <= len(digits) <= 15:
        raise ValueError("The callback did not contain a valid customer phone number.")
    return "+" + digits if has_country_prefix else digits


def _get_or_create_lead(phone: str, now: str):
    db = get_db()
    lead = db.execute("SELECT * FROM leads WHERE phone=?", (phone,)).fetchone()
    if lead:
        db.execute("UPDATE leads SET last_activity_at=?, updated_at=? WHERE id=?", (now, now, lead["id"]))
        return db.execute("SELECT * FROM leads WHERE id=?", (lead["id"],)).fetchone()
    token = secrets.token_urlsafe(24)
    cursor = db.execute(
        "INSERT INTO leads(phone,status,started_at,last_activity_at,unsubscribe_token,created_at,updated_at) VALUES(?, 'started', ?, ?, ?, ?, ?)",
        (phone, now, now, token, now, now),
    )
    return db.execute("SELECT * FROM leads WHERE id=?", (cursor.lastrowid,)).fetchone()


def handle_ussd(app, payload) -> str:
    """Handle a cumulative-text USSD callback and return a CON/END response."""
    session_id = str(payload.get("sessionId", "")).strip()
    service_code = str(payload.get("serviceCode", "")).strip()
    phone_raw = str(payload.get("phoneNumber", "")).strip()
    text = str(payload.get("text", "")).strip()
    if not session_id or len(session_id) > 160:
        raise ValueError("A valid sessionId is required.")
    if not phone_raw:
        raise ValueError("A phoneNumber is required.")
    if len(text) > 600:
        raise ValueError("The USSD response is too long.")
    phone = _normalise_phone(phone_raw)
    now = _now()
    lead = _get_or_create_lead(phone, now)
    if lead["status"] == "completed":
        return "END Your sign-up is already complete. Thank you."
    if text == "":
        code = service_code or app.config["USSD_SHORT_CODE"]
        return f"CON Welcome to {app.config['BRAND_NAME']}\n1. Continue sign-up\nDial {code} to resume later."

    parts = [part.strip() for part in text.split("*")]
    if not parts or parts[0] != "1":
        return "END Thank you. To continue sign-up, dial again and choose 1."
    if len(parts) == 1:
        return "CON Enter your full name as you want it used in email reminders."

    full_name = parts[1][:120].strip()
    if not full_name or not re.search(r"[\w\u00C0-\u024F]", full_name, re.UNICODE):
        return "CON Please enter your full name."
    if len(parts) == 2:
        return "CON Enter your email address."

    email = parts[2][:254].strip().lower()
    if not EMAIL_PATTERN.fullmatch(email):
        return "CON Enter a valid email address."
    db = get_db()
    db.execute(
        "UPDATE leads SET full_name=?, email=?, status='in_progress', last_activity_at=?, updated_at=? WHERE id=?",
        (full_name, email, now, now, lead["id"]),
    )
    if lead["email"] and lead["email"] != email:
        db.execute("UPDATE leads SET email_opt_in=0, consented_at=NULL WHERE id=?", (lead["id"],))
        db.execute(
            "UPDATE reminders SET status='suppressed', updated_at=? WHERE lead_id=? AND status IN ('pending','failed')",
            (now, lead["id"]),
        )
    if len(parts) == 3:
        return "CON Would you like email reminders if your sign-up is still incomplete?\n1. Yes\n2. No"
    if parts[3] not in {"1", "2"}:
        return "CON Choose 1 for Yes or 2 for No."

    opted_in = parts[3] == "1"
    db.execute(
        "UPDATE leads SET full_name=?, email=?, status='in_progress', email_opt_in=?, consented_at=?, unsubscribed_at=NULL, last_activity_at=?, updated_at=? WHERE id=?",
        (full_name, email, int(opted_in), now if opted_in else None, now, now, lead["id"]),
    )
    for day in (7, 30, 60):
        due_at = (datetime.fromisoformat(lead["started_at"]) + timedelta(days=day)).isoformat(timespec="seconds")
        if opted_in:
            db.execute(
                "INSERT INTO reminders(lead_id,day_after_start,scheduled_for,status,created_at,updated_at) VALUES(?,?,?,'pending',?,?) "
                "ON CONFLICT(lead_id,day_after_start) DO UPDATE SET status=CASE WHEN reminders.status='suppressed' THEN 'pending' ELSE reminders.status END, scheduled_for=excluded.scheduled_for, updated_at=excluded.updated_at",
                (lead["id"], day, due_at, now, now),
            )
        else:
            db.execute(
                "UPDATE reminders SET status='suppressed', updated_at=? WHERE lead_id=? AND day_after_start=? AND status IN ('pending','failed')",
                (now, lead["id"], day),
            )
    first_name = full_name.split()[0]
    if opted_in:
        return f"END Thank you, {first_name}. Your sign-up details are saved. Dial {app.config['USSD_SHORT_CODE']} and choose 1 to continue. We will email reminders if the sign-up remains incomplete."
    return f"END Thank you, {first_name}. Your sign-up details are saved. No reminder emails will be sent. Dial {app.config['USSD_SHORT_CODE']} and choose 1 to continue."

