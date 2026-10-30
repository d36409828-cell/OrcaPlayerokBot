"""Шина событий.

Два вида событий:
  • BOT-события жизненного цикла: INIT, POST_INIT, PLAYEROK_READY(bot), TELEGRAM_READY(tg), STOP
  • PLAYEROK-события (имена = playerokapi.enums.EventTypes):
        CHAT_INITIALIZED, NEW_MESSAGE, NEW_DEAL, NEW_REVIEW, DEAL_CONFIRMED,
        DEAL_CONFIRMED_AUTOMATICALLY, DEAL_ROLLED_BACK, DEAL_HAS_PROBLEM,
        DEAL_PROBLEM_RESOLVED, DEAL_STATUS_CHANGED, ITEM_PAID, ITEM_SENT
    обработчик вызывается как handler(bot, event)

Приоритет: чем меньше число, тем раньше запускается обработчик.
  встроенные обработчики — 100, плагины по умолчанию — 50 (то есть ДО встроенных).
Если обработчик вернул True — остальные (с большим приоритетом) НЕ вызываются.
Это позволяет плагину автопродажи «забрать» сделку у встроенной автовыдачи.

Обработчик может быть обычной функцией (запускается в потоке, можно вызывать
блокирующие методы Playerok API) или async-функцией.
"""
import asyncio
from logging import getLogger

from core.utils import maybe_await

logger = getLogger("orca.events")

BUILTIN_PRIORITY = 100
PLUGIN_PRIORITY = 50

_BOT: dict[str, list[tuple[int, object]]] = {}
_PLAYEROK: dict[str, list[tuple[int, object]]] = {}


def _add(registry: dict, event: str, fn, priority: int) -> None:
    lst = registry.setdefault(event, [])
    lst.append((priority, fn))
    lst.sort(key=lambda p: p[0])  # sort стабилен: при равном приоритете порядок регистрации


def register_bot_event(event: str, fn, priority: int = PLUGIN_PRIORITY) -> None:
    _add(_BOT, event, fn, priority)


def register_playerok_event(event: str, fn, priority: int = PLUGIN_PRIORITY) -> None:
    _add(_PLAYEROK, event, fn, priority)


def on_playerok_event(event: str, priority: int = BUILTIN_PRIORITY):
    def deco(fn):
        register_playerok_event(event, fn, priority)
        return fn
    return deco


def on_bot_event(event: str, priority: int = BUILTIN_PRIORITY):
    def deco(fn):
        register_bot_event(event, fn, priority)
        return fn
    return deco


async def _run(fn, args):
    if asyncio.iscoroutinefunction(fn):
        return await fn(*args)
    return await maybe_await(await asyncio.to_thread(fn, *args))


async def _dispatch(registry: dict, event: str, args: list):
    for _prio, fn in list(registry.get(event, [])):
        try:
            if await _run(fn, args) is True:
                logger.debug(f"{event}: обработка остановлена обработчиком {getattr(fn, '__name__', fn)}")
                return True
        except Exception:
            logger.exception(f"Ошибка в обработчике {getattr(fn, '__module__', '?')}."
                             f"{getattr(fn, '__name__', fn)} (событие {event})")
    return False


async def call_bot_event(event: str, args: list | None = None):
    return await _dispatch(_BOT, event, args or [])


async def call_playerok_event(event: str, args: list | None = None):
    return await _dispatch(_PLAYEROK, event, args or [])
