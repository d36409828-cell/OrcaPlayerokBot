"""Хуки статистики сетевых ошибок: их ждёт playerokapi.account (необязательные)."""
from collections import deque
from logging import getLogger
import time

logger = getLogger("orca.errors")
_recent: deque = deque(maxlen=200)


def record_playerok_request_error(*args, **kwargs) -> None:
    _recent.append((time.time(), str(args)[:200], str(kwargs)[:200]))


def record_playerok_request_success(*args, **kwargs) -> None:
    pass


def recent_errors_count(window_seconds: int = 600) -> int:
    limit = time.time() - window_seconds
    return sum(1 for t, *_ in _recent if t >= limit)
