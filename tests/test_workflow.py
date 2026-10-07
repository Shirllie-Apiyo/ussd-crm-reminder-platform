from __future__ import annotations

import re
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from reminder_platform import create_app
from reminder_platform.db import get_db
from reminder_platform.emailing import build_reminder_message
from reminder_platform.emailing import deliver_reminder
from reminder_platform.reminders import process_due_reminders
from tests.helpers import PHONE, signup, ussd


class ReminderPlatformTests(unittest.TestCase):
    def setUp(self):
        database_path = Path(__file__).resolve().parents[1] / "instance" / "unittest.sqlite3"
        self.app = create_app({
            "TESTING": True,
            "START_SCHEDULER": False,
            "DATABASE_PATH": str(database_path),
            "FLASK_SECRET_KEY": "test-key-with-adequate-length",
            "ADMIN_USERNAME": "team-admin",
            "ADMIN_PASSWORD": "test-password-123",
            "APP_ENV": "test",
            "EMAIL_BACKEND": "console",
            "USSD_WEBHOOK_TOKEN": "",
            "PUBLIC_BASE_URL": "https://crm.example.test",
            "BRAND_NAME": "Kifaa",
            "USSD_SHORT_CODE": "*456#",
        })
        self.client = self.app.test_client()
        with self.app.app_context():
            db = get_db()
            db.execute("DELETE FROM reminders")
            db.execute("DELETE FROM leads")
            db.commit()

    def tearDown(self):
        stop_event = self.app.extensions.get("reminder_scheduler_stop")
        if stop_event:
            stop_event.set()

    def test_ussd_captures_name_email_and_explicit_reminder_consent(self):
        responses = signup(self.client)
        self.assertTrue(responses[0].get_data(as_text=True).startswith("CON Welcome"))
        self.assertIn("full name", responses[1].get_data(as_text=True))
        self.assertIn("email address", responses[2].get_data(as_text=True))
        self.assertIn("email reminders", responses[3].get_data(as_text=True))
        self.assertTrue(responses[4].get_data(as_text=True).startswith("END Thank you, Samira"))
        with self.app.app_context():
            db = get_db()
            lead = db.execute("SELECT * FROM leads WHERE phone=?", (PHONE,)).fetchone()
            reminders = db.execute("SELECT * FROM reminders WHERE lead_id=? ORDER BY day_after_start", (lead["id"],)).fetchall()
        self.assertEqual(lead["full_name"], "Samira Njeri")
        self.assertEqual(lead["email"], "samira@example.com")
        self.assertEqual(lead["email_opt_in"], 1)
        self.assertEqual([row["day_after_start"] for row in reminders], [7, 30, 60])

    def test_invalid_email_is_rejected_and_no_reminder_is_created(self):
        ussd(self.client, "")
        ussd(self.client, "1")
        ussd(self.client, "1*Samira Njeri")
        response = ussd(self.client, "1*Samira Njeri*not-an-email")
        self.assertIn("valid email", response.get_data(as_text=True))
        with self.app.app_context():
            self.assertEqual(get_db().execute("SELECT COUNT(*) FROM reminders").fetchone()[0], 0)

    def test_declined_consent_stores_progress_but_suppresses_all_email(self):
        signup(self.client, consent="2", phone="+254700000112")
        with self.app.app_context():
            db = get_db()
            lead = db.execute("SELECT * FROM leads WHERE phone=?", ("+254700000112",)).fetchone()
            self.assertEqual(lead["status"], "in_progress")
            self.assertEqual(lead["email_opt_in"], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM reminders WHERE lead_id=?", (lead["id"],)).fetchone()[0], 0)

    def test_reminders_fire_once_at_days_7_30_and_60(self):
        signup(self.client)
        with self.app.app_context():
            lead = dict(get_db().execute("SELECT * FROM leads WHERE phone=?", (PHONE,)).fetchone())
        start = datetime.fromisoformat(lead["started_at"])
        first = process_due_reminders(self.app, now=start + timedelta(days=8))
        repeated = process_due_reminders(self.app, now=start + timedelta(days=8, minutes=2))
        second = process_due_reminders(self.app, now=start + timedelta(days=31))
        third = process_due_reminders(self.app, now=start + timedelta(days=61))
        self.assertEqual(first["previewed"], 1)
        self.assertEqual(repeated["previewed"], 0)
        self.assertEqual(second["previewed"], 1)
        self.assertEqual(third["previewed"], 1)
        with self.app.app_context():
            rows = get_db().execute("SELECT day_after_start,status FROM reminders ORDER BY day_after_start").fetchall()
        self.assertEqual([(row["day_after_start"], row["status"]) for row in rows], [(7, "previewed"), (30, "previewed"), (60, "previewed")])

    def test_email_is_personalized_and_html_escapes_customer_name(self):
        signup(self.client, name="Samira <script>alert(1)</script>")
        with self.app.app_context():
            lead = get_db().execute("SELECT * FROM leads WHERE phone=?", (PHONE,)).fetchone()
            _subject, text, html_body = build_reminder_message(self.app, lead, 7)
        self.assertTrue(text.startswith("Dear Samira <script>alert(1)</script>,"))
        self.assertIn("&lt;script&gt;", html_body)
        self.assertNotIn("<script>alert(1)</script>", html_body)

    def test_smtp_backend_sends_personalized_email(self):
        signup(self.client)
        with self.app.app_context():
            lead = get_db().execute("SELECT * FROM leads WHERE phone=?", (PHONE,)).fetchone()
            self.app.config.update(
                EMAIL_BACKEND="smtp",
                SMTP_HOST="smtp.example.test",
                SMTP_PORT=587,
                SMTP_USERNAME="",
                SMTP_PASSWORD="",
                SMTP_USE_TLS=True,
                MAIL_FROM="Kifaa Team <no-reply@example.test>",
            )
            smtp = MagicMock()
            smtp.__enter__.return_value = smtp
            with patch("reminder_platform.emailing.smtplib.SMTP", return_value=smtp):
                result = deliver_reminder(self.app, lead, 7)
        self.assertEqual(result, "sent")
        message = smtp.send_message.call_args.args[0]
        self.assertEqual(message["To"], "samira@example.com")
        self.assertIn("Dear Samira Njeri,", message.get_body(preferencelist=("plain",)).get_content())
        self.assertIn("samira@example.com", message["To"])
        smtp.starttls.assert_called_once()

    def test_unsubscribe_suppresses_pending_reminders(self):
        signup(self.client)
        with self.app.app_context():
            lead = dict(get_db().execute("SELECT * FROM leads WHERE phone=?", (PHONE,)).fetchone())
        page = self.client.get(f"/unsubscribe/{lead['unsubscribe_token']}")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.get_data(as_text=True)).group(1)
        result = self.client.post(f"/unsubscribe/{lead['unsubscribe_token']}", data={"csrf_token": csrf})
        self.assertEqual(result.status_code, 200)
        with self.app.app_context():
            db = get_db()
            updated = db.execute("SELECT email_opt_in FROM leads WHERE id=?", (lead["id"],)).fetchone()
            states = db.execute("SELECT DISTINCT status FROM reminders WHERE lead_id=?", (lead["id"],)).fetchall()
        self.assertEqual(updated["email_opt_in"], 0)
        self.assertEqual([state["status"] for state in states], ["suppressed"])

    def test_dashboard_requires_login(self):
        self.assertEqual(self.client.get("/").status_code, 302)
        signup(self.client)
        page = self.client.get("/login")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.get_data(as_text=True)).group(1)
        signed_in = self.client.post(
            "/login",
            data={"username": "team-admin", "password": "test-password-123", "csrf_token": csrf},
            follow_redirects=True,
        )
        self.assertEqual(signed_in.status_code, 200)
        self.assertIn("Signup pipeline", signed_in.get_data(as_text=True))
        self.assertIn("Samira Njeri", signed_in.get_data(as_text=True))
        with self.app.app_context():
            lead_id = get_db().execute("SELECT id FROM leads WHERE phone=?", (PHONE,)).fetchone()["id"]
        detail = self.client.get(f"/leads/{lead_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn("Day 30 reminder", detail.get_data(as_text=True))

    def test_staff_completion_stops_all_future_reminders(self):
        signup(self.client)
        with self.app.app_context():
            lead = dict(get_db().execute("SELECT * FROM leads WHERE phone=?", (PHONE,)).fetchone())
        login_page = self.client.get("/login")
        login_csrf = re.search(r'name="csrf_token" value="([^"]+)"', login_page.get_data(as_text=True)).group(1)
        self.client.post("/login", data={"username": "team-admin", "password": "test-password-123", "csrf_token": login_csrf})
        detail = self.client.get(f"/leads/{lead['id']}")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', detail.get_data(as_text=True)).group(1)
        result = self.client.post(f"/leads/{lead['id']}/complete", data={"csrf_token": csrf})
        self.assertEqual(result.status_code, 302)
        with self.app.app_context():
            db = get_db()
            self.assertEqual(db.execute("SELECT status FROM leads WHERE id=?", (lead["id"],)).fetchone()["status"], "completed")
            statuses = db.execute("SELECT DISTINCT status FROM reminders WHERE lead_id=?", (lead["id"],)).fetchall()
        self.assertEqual([row["status"] for row in statuses], ["suppressed"])
        due = datetime.fromisoformat(lead["started_at"]) + timedelta(days=61)
        outcome = process_due_reminders(self.app, now=due)
        self.assertEqual(outcome["previewed"], 0)


if __name__ == "__main__":
    unittest.main()

