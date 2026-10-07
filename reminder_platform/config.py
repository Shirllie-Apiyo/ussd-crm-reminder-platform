from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEVELOPMENT_ADMIN_PASSWORD = "ChangeThisLocalPassword123!"


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key.replace("_", "").isalnum():
            os.environ.setdefault(key, value)


_load_dotenv(ROOT / ".env")


class Settings:
    FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "replace-with-a-long-random-secret")
    ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", DEVELOPMENT_ADMIN_PASSWORD)
    DATABASE_PATH = os.getenv("DATABASE_PATH", str(ROOT / "instance" / "crm.sqlite3"))
    APP_ENV = os.getenv("APP_ENV", "development").lower()
    BRAND_NAME = os.getenv("BRAND_NAME", "Kifaa Sign-up")
    USSD_SHORT_CODE = os.getenv("USSD_SHORT_CODE", "*123#")
    USSD_WEBHOOK_TOKEN = os.getenv("USSD_WEBHOOK_TOKEN", "")
    REMINDER_CHECK_SECONDS = int(os.getenv("REMINDER_CHECK_SECONDS", "60"))
    START_SCHEDULER = os.getenv("START_SCHEDULER", "true").lower() in {"1", "true", "yes"}
    EMAIL_BACKEND = os.getenv("EMAIL_BACKEND", "console").lower()
    MAIL_FROM = os.getenv("MAIL_FROM", "Sign-up Team <no-reply@example.com>")
    SMTP_HOST = os.getenv("SMTP_HOST", "")
    SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
    SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() in {"1", "true", "yes"}
    SUPPORT_EMAIL = os.getenv("SUPPORT_EMAIL", "support@example.com")
    PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:5000")
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

