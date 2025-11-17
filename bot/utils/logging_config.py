from __future__ import annotations

import logging
from logging.config import dictConfig


def setup_logging(level: int = logging.INFO) -> None:
    config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": "%(asctime)s | %(name)s | %(levelname)s | %(message)s",
            }
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "default",
                "level": level,
            }
        },
        "loggers": {
            "bot": {
                "handlers": ["console"],
                "level": level,
                "propagate": False,
            }
        },
        "root": {
            "handlers": ["console"],
            "level": level,
        },
    }

    dictConfig(config)
