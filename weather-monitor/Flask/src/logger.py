import logging
import os
import sys

_LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "normal": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def setup_logging():
    mode = os.environ.get("APP_LOG_MODE", "info").lower()
    level = _LOG_LEVELS.get(mode, logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)

    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    if level == logging.DEBUG:
        fmt = "%(asctime)s [%(levelname)s] %(name)s (%(filename)s:%(lineno)d): %(message)s"

    handler.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))

    root = logging.getLogger()
    root.setLevel(level)
    # Replace any existing handlers (gunicorn adds its own).
    root.handlers = [handler]

    # Silence noisy third-party loggers unless we're in debug mode.
    if level > logging.DEBUG:
        for noisy in ("google.auth", "google.api_core", "urllib3", "werkzeug"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

    logging.getLogger("voice_assistant").info(
        "Logging initialised — level=%s (set APP_LOG_MODE=debug for verbose output)",
        logging.getLevelName(level),
    )
