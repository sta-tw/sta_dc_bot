from __future__ import annotations

import logging
from collections import deque
from logging.config import dictConfig

class _InMemoryLogHandler(logging.Handler):

    def __init__(self, buffer: deque[str], level: int) -> None:
        super().__init__(level=level)
        self._buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            message = record.getMessage()
        self._buffer.append(message)

_LOG_BUFFER: deque[str] = deque(maxlen=200)
_MEMORY_HANDLER = _InMemoryLogHandler(_LOG_BUFFER, level=logging.INFO)

def setup_logging(level: int = logging.INFO) -> None:
    config = {
        'version': 1,
        'disable_existing_loggers': False,
        'formatters': {
            'default': {
                'format': "%(asctime)s | %(name)s | %(levelname)s | %(message)s",
            }
        },
        'handlers': {
            'console': {
                'class': "logging.StreamHandler",
                'formatter': "default",
                'level': level,
            },
            'memory': {
                '()': lambda: _MEMORY_HANDLER,
                'formatter': "default",
                'level': level,
            },
        },
        'loggers': {
            '': {
                'handlers': ["console", "memory"],
                'level': level,
                'propagate': False,
            }
        },
        'root': {
            'handlers': ["console", "memory"],
            'level': level,
        },
    }

    dictConfig(config)

def get_recent_logs(limit: int = 25) -> list[str]:
    if limit <= 0:
        return []
    return list(_LOG_BUFFER)[-limit:]
