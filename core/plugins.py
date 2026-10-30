"""Загрузка плагинов из папки plugins/.

Плагин — обычный .py файл (или папка с __init__.py). Минимальный пример:

    META = {"name": "Мой плагин", "version": "1.0", "description": "...", "author": "..."}

    def init(ctx):                      # необязательно; вызывается при подключении
        ctx.logger.info("hello")

    def on_new_deal(bot, event):        # bot = PlayerokBot, event = NewDealEvent
        ...
        return True                     # True = «я сам обработал», встроенные обработчики пропустят

    PLAYEROK_EVENT_HANDLERS = {"NEW_DEAL": [on_new_deal]}
    BOT_EVENT_HANDLERS = {"POST_INIT": [...]}          # необязательно

Для приоритета вместо функции можно указать кортеж (priority, функция).
"""
import importlib.util
import os
import sys
from dataclasses import dataclass, field
from logging import getLogger

import paths
from core import state
from core.handlers import (PLUGIN_PRIORITY, register_bot_event, register_playerok_event)
from settings import Settings

logger = getLogger("orca.plugins")


@dataclass
class Plugin:
    id: str
    path: str
    module: object = None
    name: str = ""
    version: str = "?"
    description: str = ""
    author: str = ""
    enabled: bool = True
    error: str | None = None
    ctx: "PluginContext | None" = field(default=None, repr=False)


class PluginContext:
    """Всё, что плагину нужно от бота."""

    def __init__(self, plugin: Plugin):
        self._plugin = plugin
        self.logger = getLogger(f"plugin.{plugin.id}")
        self.data_dir = os.path.join(paths.PLUGINS_DATA_DIR, plugin.id)
        os.makedirs(self.data_dir, exist_ok=True)
        self.settings = Settings
        self.telegram_routers: list = []

    # живые объекты
    @property
    def playerok_bot(self): return state.playerok_bot
    @property
    def telegram_bot(self): return state.telegram_bot
    @property
    def account(self): return state.playerok_bot.account if state.playerok_bot else None

    # регистрация обработчиков «на лету»
    def on_playerok_event(self, event: str, fn, priority: int = PLUGIN_PRIORITY):
        register_playerok_event(event, fn, priority)

    def on_bot_event(self, event: str, fn, priority: int = PLUGIN_PRIORITY):
        register_bot_event(event, fn, priority)

    def add_telegram_router(self, router) -> None:
        """Подключить aiogram Router со своими командами/кнопками к Telegram-панели."""
        self.telegram_routers.append(router)

    # удобные действия (блокирующие — вызывать из обычных функций-обработчиков)
    def send_message(self, chat_id: str, text: str) -> None:
        self.playerok_bot.send_message(chat_id, text)

    def notify(self, text: str) -> None:
        """Отправить уведомление в Telegram всем авторизованным пользователям."""
        import asyncio
        if state.telegram_bot and state.main_loop:
            asyncio.run_coroutine_threadsafe(state.telegram_bot.notify(text), state.main_loop)


def _read_meta(mod, plugin: Plugin) -> None:
    meta = getattr(mod, "META", {}) or {}
    plugin.name = str(meta.get("name") or getattr(mod, "NAME", plugin.id))
    plugin.version = str(meta.get("version") or getattr(mod, "VERSION", "?"))
    plugin.description = str(meta.get("description") or getattr(mod, "DESCRIPTION", ""))
    plugin.author = str(meta.get("author") or getattr(mod, "AUTHOR", ""))


def load_plugins() -> list[Plugin]:
    os.makedirs(paths.PLUGINS_DIR, exist_ok=True)
    disabled = set(Settings.get("config")["plugins"]["disabled"])
    plugins: list[Plugin] = []
    for entry in sorted(os.listdir(paths.PLUGINS_DIR)):
        full = os.path.join(paths.PLUGINS_DIR, entry)
        if entry.startswith(("_", ".")):
            continue
        if os.path.isfile(full) and entry.endswith(".py"):
            pid, file = entry[:-3], full
        elif os.path.isdir(full) and os.path.exists(os.path.join(full, "__init__.py")):
            pid, file = entry, os.path.join(full, "__init__.py")
        else:
            continue
        plugin = Plugin(id=pid, path=full, name=pid, enabled=pid not in disabled)
        plugins.append(plugin)
        if not plugin.enabled:
            continue
        try:
            spec = importlib.util.spec_from_file_location(
                f"orca_plugin_{pid}", file,
                submodule_search_locations=[full] if os.path.isdir(full) else None)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
            plugin.module = mod
            _read_meta(mod, plugin)
        except Exception as e:
            plugin.error = f"{type(e).__name__}: {e}"
            logger.exception(f"Не удалось загрузить плагин {pid}")
    state.plugins = plugins
    return plugins


def _register(registry_fn, handlers: dict) -> None:
    for event, fns in (handlers or {}).items():
        for item in (fns if isinstance(fns, (list, tuple)) and not _is_prio(fns) else [fns]):
            prio, fn = item if _is_prio(item) else (PLUGIN_PRIORITY, item)
            registry_fn(event, fn, prio)


def _is_prio(x) -> bool:
    return isinstance(x, tuple) and len(x) == 2 and isinstance(x[0], int) and callable(x[1])


async def connect_plugins(plugins: list[Plugin]) -> None:
    for plugin in plugins:
        if not plugin.enabled or plugin.module is None:
            continue
        try:
            plugin.ctx = PluginContext(plugin)
            mod = plugin.module
            _register(register_bot_event, getattr(mod, "BOT_EVENT_HANDLERS", {}))
            _register(register_playerok_event, getattr(mod, "PLAYEROK_EVENT_HANDLERS", {}))
            init = getattr(mod, "init", None)
            if callable(init):
                import asyncio
                res = init(plugin.ctx)
                if asyncio.iscoroutine(res):
                    await res
            logger.info(f"Плагин подключён: {plugin.name} v{plugin.version}")
        except Exception as e:
            plugin.error = f"{type(e).__name__}: {e}"
            logger.exception(f"Ошибка подключения плагина {plugin.id}")
