from __future__ import annotations

import hmac
import logging
import secrets
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, session, url_for

from .config import DEVELOPMENT_ADMIN_PASSWORD, Settings
from .db import close_db, get_db, init_db
from .emailing import deliver_reminder
from .reminders import process_due_reminders, start_scheduler
from .ussd import handle_ussd


def _secure_compare(first: str, second: str) -> bool:
    return hmac.compare_digest(str(first).encode("utf-8"), str(second).encode("utf-8"))


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(Settings)
    if test_config:
        app.config.update(test_config)

    if app.config["EMAIL_BACKEND"] not in {"console", "smtp"}:
        raise RuntimeError("EMAIL_BACKEND must be 'console' or 'smtp'.")
    if app.config["APP_ENV"] == "production" and app.config["EMAIL_BACKEND"] != "smtp":
        raise RuntimeError("Set EMAIL_BACKEND=smtp before running in production.")
    if app.config["EMAIL_BACKEND"] == "smtp" and not app.config["SMTP_HOST"]:
        raise RuntimeError("Set SMTP_HOST when EMAIL_BACKEND=smtp.")

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    db_path = Path(app.config["DATABASE_PATH"])
    db_path.parent.mkdir(parents=True, exist_ok=True)
    app.teardown_appcontext(close_db)
    init_db(app)

    @app.after_request
    def commit_successful_request(response):
        db = g.get("db")
        if db is not None:
            if response.status_code < 400:
                db.commit()
            else:
                db.rollback()
        return response

    logging.basicConfig(level=getattr(logging, app.config["LOG_LEVEL"].upper(), logging.INFO))
    if app.config["APP_ENV"] == "production":
        if app.config["FLASK_SECRET_KEY"] == "replace-with-a-long-random-secret":
            raise RuntimeError("Set FLASK_SECRET_KEY before running in production.")
        if not app.config["USSD_WEBHOOK_TOKEN"]:
            raise RuntimeError("Set USSD_WEBHOOK_TOKEN before exposing the USSD webhook in production.")
        if app.config["ADMIN_PASSWORD"] == DEVELOPMENT_ADMIN_PASSWORD:
            raise RuntimeError("Set a unique ADMIN_PASSWORD before running in production.")

    app.secret_key = app.config["FLASK_SECRET_KEY"]
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=app.config["APP_ENV"] == "production",
        MAX_CONTENT_LENGTH=32 * 1024,
    )
    app.jinja_env.filters["localdate"] = lambda value: _format_date(value)
    app.jinja_env.filters["adddays"] = lambda value, days: _add_days(value, days)
    app.jinja_env.filters["nextmilestone"] = lambda value: _next_milestone(value)

    if app.config["START_SCHEDULER"]:
        start_scheduler(app)

    @app.context_processor
    def inject_template_values():
        if "_csrf_token" not in session:
            session["_csrf_token"] = secrets.token_urlsafe(24)
        return {"csrf_token": session["_csrf_token"], "brand_name": app.config["BRAND_NAME"]}

    @app.before_request
    def protect_dashboard():
        public_endpoints = {"login", "ussd_webhook", "health", "unsubscribe", "unsubscribe_done", "static"}
        if request.endpoint in public_endpoints:
            return None
        if not session.get("admin_authenticated"):
            return redirect(url_for("login", next=request.path))
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            supplied = request.form.get("csrf_token", "") or request.headers.get("X-CSRF-Token", "")
            expected = session.get("_csrf_token", "")
            if not supplied or not expected or not _secure_compare(supplied, expected):
                abort(400, description="The form expired. Reload the page and try again.")
        return None

    def validate_login_csrf() -> None:
        supplied = request.form.get("csrf_token", "")
        expected = session.get("_csrf_token", "")
        if not supplied or not expected or not _secure_compare(supplied, expected):
            abort(400, description="The sign-in form expired. Reload and try again.")

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if session.get("admin_authenticated"):
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            validate_login_csrf()
            username = request.form.get("username", "")
            password = request.form.get("password", "")
            if _secure_compare(username, app.config["ADMIN_USERNAME"]) and _secure_compare(
                password, app.config["ADMIN_PASSWORD"]
            ):
                session.clear()
                session["admin_authenticated"] = True
                session["_csrf_token"] = secrets.token_urlsafe(24)
                return redirect(url_for("dashboard"))
            flash("The username or password was not recognized.", "error")
        return render_template("login.html")

    @app.post("/logout")
    def logout():
        session.clear()
        flash("You have signed out.", "success")
        return redirect(url_for("login"))

    @app.get("/")
    def dashboard():
        db = get_db()
        stats = {
            "total": db.execute("SELECT COUNT(*) FROM leads").fetchone()[0],
            "active": db.execute("SELECT COUNT(*) FROM leads WHERE status != 'completed'").fetchone()[0],
            "eligible": db.execute(
                "SELECT COUNT(*) FROM leads WHERE status = 'in_progress' AND email IS NOT NULL AND email_opt_in = 1"
            ).fetchone()[0],
            "sent": db.execute("SELECT COUNT(*) FROM reminders WHERE status = 'sent'").fetchone()[0],
            "previewed": db.execute("SELECT COUNT(*) FROM reminders WHERE status = 'previewed'").fetchone()[0],
        }
        search = request.args.get("q", "").strip()
        query = """SELECT l.*,
            (SELECT MIN(scheduled_for) FROM reminders r WHERE r.lead_id=l.id AND r.status IN ('pending','failed')) AS next_reminder,
            (SELECT COUNT(*) FROM reminders r WHERE r.lead_id=l.id AND r.status IN ('sent','previewed')) AS reminder_count
            FROM leads l"""
        args: list[str] = []
        if search:
            query += " WHERE l.full_name LIKE ? OR l.phone LIKE ? OR l.email LIKE ?"
            pattern = f"%{search}%"
            args.extend([pattern, pattern, pattern])
        query += " ORDER BY CASE l.status WHEN 'in_progress' THEN 0 WHEN 'started' THEN 1 ELSE 2 END, l.started_at DESC LIMIT 200"
        leads = db.execute(query, args).fetchall()
        return render_template("dashboard.html", stats=stats, leads=leads, search=search, email_backend=app.config["EMAIL_BACKEND"])

    @app.get("/leads/<int:lead_id>")
    def lead_detail(lead_id: int):
        db = get_db()
        lead = db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if lead is None:
            abort(404)
        reminders = db.execute(
            "SELECT * FROM reminders WHERE lead_id=? ORDER BY day_after_start", (lead_id,)
        ).fetchall()
        return render_template("lead.html", lead=lead, reminders=reminders)

    @app.post("/leads/<int:lead_id>/complete")
    def complete_lead(lead_id: int):
        db = get_db()
        now = datetime.now(UTC).isoformat(timespec="seconds")
        cursor = db.execute(
            "UPDATE leads SET status='completed', completed_at=?, updated_at=? WHERE id=? AND status!='completed'",
            (now, now, lead_id),
        )
        if cursor.rowcount == 0 and db.execute("SELECT 1 FROM leads WHERE id=?", (lead_id,)).fetchone() is None:
            abort(404)
        db.execute("UPDATE reminders SET status='suppressed', updated_at=? WHERE lead_id=? AND status IN ('pending','failed')", (now, lead_id))
        flash("Signup marked complete. Pending reminders will be skipped.", "success")
        return redirect(url_for("lead_detail", lead_id=lead_id))

    @app.route("/unsubscribe/<token>", methods=["GET", "POST"])
    def unsubscribe(token: str):
        db = get_db()
        lead = db.execute("SELECT id, full_name, email_opt_in FROM leads WHERE unsubscribe_token=?", (token,)).fetchone()
        if lead is None:
            abort(404)
        if request.method == "POST":
            supplied = request.form.get("csrf_token", "")
            expected = session.get("_csrf_token", "")
            if not supplied or not expected or not hmac.compare_digest(supplied, expected):
                abort(400)
            now = datetime.now(UTC).isoformat(timespec="seconds")
            db.execute("UPDATE leads SET email_opt_in=0, unsubscribed_at=?, updated_at=? WHERE id=?", (now, now, lead["id"]))
            db.execute("UPDATE reminders SET status='suppressed', updated_at=? WHERE lead_id=? AND status IN ('pending','failed')", (now, lead["id"]))
            return render_template("unsubscribe_done.html", name=lead["full_name"])
        return render_template("unsubscribe.html", lead=lead)

    @app.post("/webhooks/ussd")
    def ussd_webhook():
        expected = app.config["USSD_WEBHOOK_TOKEN"]
        if expected:
            supplied = request.headers.get("X-Webhook-Token", "")
            if not supplied or not _secure_compare(supplied, expected):
                abort(401)
        payload = request.get_json(silent=True) if request.is_json else request.form
        if not payload:
            abort(400, description="Send the USSD callback fields as JSON or form data.")
        try:
            reply = handle_ussd(app, payload)
        except ValueError as error:
            abort(400, description=str(error))
        return app.response_class(reply, mimetype="text/plain")

    app.extensions["process_due_reminders"] = lambda now=None: process_due_reminders(app, now=now)
    return app


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _format_date(value: str | None) -> str:
    if not value:
        return "—"
    return _parse_datetime(value).strftime("%d %b %Y")


def _add_days(value: str, days: int) -> str:
    return (_parse_datetime(value) + timedelta(days=days)).isoformat(timespec="seconds")


def _next_milestone(value: str) -> str:
    now = datetime.now(UTC)
    started = _parse_datetime(value)
    for day in (7, 30, 60):
        due = started + timedelta(days=day)
        if due > now:
            return due.strftime("%d %b %Y")
    return "Due now"


__all__ = ["create_app", "process_due_reminders", "deliver_reminder"]

