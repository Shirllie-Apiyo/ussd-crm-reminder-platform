from __future__ import annotations

import sqlite3
from pathlib import Path

from flask import current_app, g


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT NOT NULL UNIQUE,
    full_name TEXT,
    email TEXT,
    status TEXT NOT NULL DEFAULT 'started' CHECK(status IN ('started','in_progress','completed')),
    email_opt_in INTEGER NOT NULL DEFAULT 0 CHECK(email_opt_in IN (0,1)),
    started_at TEXT NOT NULL,
    last_activity_at TEXT NOT NULL,
    consented_at TEXT,
    completed_at TEXT,
    unsubscribed_at TEXT,
    unsubscribe_token TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leads_status_due ON leads(status, email_opt_in, started_at);
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    day_after_start INTEGER NOT NULL CHECK(day_after_start IN (7,30,60)),
    scheduled_for TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sending','sent','previewed','failed','suppressed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    delivered_at TEXT,
    retry_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(lead_id, day_after_start)
);
CREATE INDEX IF NOT EXISTS idx_reminders_queue ON reminders(status, scheduled_for, retry_at);
"""


def _database_path() -> str:
    return str(current_app.config["DATABASE_PATH"])


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(_database_path(), timeout=15)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA busy_timeout = 15000")
    return g.db


def close_db(_error: BaseException | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(app) -> None:
    path = Path(app.config["DATABASE_PATH"])
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    try:
        db.executescript(SCHEMA)
        db.commit()
    finally:
        db.close()

