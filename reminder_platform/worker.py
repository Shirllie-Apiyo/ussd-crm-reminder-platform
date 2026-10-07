from __future__ import annotations

import logging
import threading

from . import create_app
from .reminders import process_due_reminders


def main() -> None:
    app = create_app({"START_SCHEDULER": False})
    interval = max(15, int(app.config["REMINDER_CHECK_SECONDS"]))
    stop = threading.Event()
    logging.basicConfig(level=getattr(logging, app.config["LOG_LEVEL"].upper(), logging.INFO))
    logging.getLogger(__name__).info("Signup reminder worker started (interval=%s seconds).", interval)
    try:
        while not stop.is_set():
            try:
                process_due_reminders(app)
            except Exception:
                logging.getLogger(__name__).exception("Reminder worker tick failed.")
            stop.wait(interval)
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Signup reminder worker stopped.")


if __name__ == "__main__":
    main()

