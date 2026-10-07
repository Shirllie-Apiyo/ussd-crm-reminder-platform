from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from threading import Thread

from .emailing import deliver_reminder


logger = logging.getLogger(__name__)
MILESTONES = (7, 30, 60)


def _as_utc(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        return current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _open_db(path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 15000")
    return connection


def process_due_reminders(app, now: datetime | None = None) -> dict[str, int]:
    """Send the oldest due reminder per opted-in incomplete lead; safe to rerun."""
    current = _as_utc(now)
    stamp = current.isoformat(timespec="seconds")
    result = {"sent": 0, "previewed": 0, "failed": 0, "skipped": 0}
    with app.app_context():
        connection = _open_db(app.config["DATABASE_PATH"])
        try:
            leads = connection.execute(
                "SELECT * FROM leads WHERE status='in_progress' AND email IS NOT NULL AND email_opt_in=1 ORDER BY started_at"
            ).fetchall()
            for lead in leads:
                if not lead["email"] or not lead["full_name"]:
                    result["skipped"] += 1
                    continue

                # Recover a worker interrupted during SMTP before retrying it.
                stale_before = (current - timedelta(minutes=15)).isoformat(timespec="seconds")
                connection.execute(
                    "UPDATE reminders SET status='failed', retry_at=?, last_error='Previous delivery process stopped', updated_at=? "
                    "WHERE lead_id=? AND status='sending' AND updated_at<?",
                    (stamp, stamp, lead["id"], stale_before),
                )
                recent = connection.execute(
                    "SELECT delivered_at FROM reminders WHERE lead_id=? AND status IN ('sent','previewed') ORDER BY delivered_at DESC LIMIT 1",
                    (lead["id"],),
                ).fetchone()
                if recent and recent["delivered_at"] and _parse_time(recent["delivered_at"]) > current - timedelta(hours=24):
                    continue

                for day in MILESTONES:
                    due_at = _parse_time(lead["started_at"]) + timedelta(days=day)
                    if due_at > current:
                        break
                    reminder = connection.execute(
                        "SELECT * FROM reminders WHERE lead_id=? AND day_after_start=?", (lead["id"], day)
                    ).fetchone()
                    if reminder and reminder["status"] in {"sent", "previewed", "suppressed"}:
                        continue
                    if reminder and reminder["status"] == "failed" and reminder["retry_at"] and _parse_time(reminder["retry_at"]) > current:
                        break
                    if reminder and reminder["status"] == "sending":
                        break

                    scheduled = due_at.isoformat(timespec="seconds")
                    if reminder is None:
                        connection.execute(
                            "INSERT INTO reminders(lead_id,day_after_start,scheduled_for,status,created_at,updated_at) VALUES(?,?,?,'pending',?,?)",
                            (lead["id"], day, scheduled, stamp, stamp),
                        )
                        reminder = connection.execute(
                            "SELECT * FROM reminders WHERE lead_id=? AND day_after_start=?", (lead["id"], day)
                        ).fetchone()
                    attempt = reminder["attempts"] + 1
                    connection.execute(
                        "UPDATE reminders SET status='sending', attempts=?, retry_at=NULL, last_error=NULL, updated_at=? WHERE id=?",
                        (attempt, stamp, reminder["id"]),
                    )
                    connection.commit()

                    try:
                        delivery_status = deliver_reminder(app, lead, day)
                        connection.execute(
                            "UPDATE reminders SET status=?, delivered_at=?, updated_at=? WHERE id=?",
                            (delivery_status, stamp, stamp, reminder["id"]),
                        )
                        connection.commit()
                        result[delivery_status] = result.get(delivery_status, 0) + 1
                    except Exception as error:  # Keep sensitive provider responses out of logs and the CRM.
                        retry_minutes = min(60, 5 * (2 ** min(attempt - 1, 4)))
                        retry_at = (current + timedelta(minutes=retry_minutes)).isoformat(timespec="seconds")
                        connection.execute(
                            "UPDATE reminders SET status='failed', retry_at=?, last_error=?, updated_at=? WHERE id=?",
                            (retry_at, type(error).__name__, stamp, reminder["id"]),
                        )
                        connection.commit()
                        logger.warning("Reminder delivery failed (lead_id=%s, milestone=%s, error=%s).", lead["id"], day, type(error).__name__)
                        result["failed"] += 1
                    break  # Avoid sending several overdue messages to one person in one scheduler tick.
        finally:
            connection.close()
    return result


def start_scheduler(app) -> None:
    stop_event = threading.Event()
    interval = max(15, int(app.config["REMINDER_CHECK_SECONDS"]))

    def run() -> None:
        while not stop_event.wait(interval):
            try:
                process_due_reminders(app)
            except Exception:
                logger.exception("Reminder scheduler tick failed.")

    thread = Thread(target=run, name="signup-reminder-scheduler", daemon=True)
    thread.start()
    app.extensions["reminder_scheduler_stop"] = stop_event
    app.extensions["reminder_scheduler_thread"] = thread

