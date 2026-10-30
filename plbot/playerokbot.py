"""Подключение к Playerok: аккаунт, слушатель событий, отправка сообщений."""
import asyncio
import threading
import time
from logging import getLogger

from core import state
from core.handlers import call_bot_event, call_playerok_event
from settings import Settings

from playerokapi.account import Account
from playerokapi.exceptions import CloudflareDetectedException, UnauthorizedError
from playerokapi.listener.listener import EventListener

logger = getLogger("orca.playerok")

HEALTH_CHECK_SECONDS = 300


def format_text(template: str, **values) -> str:
    """Подставляет {ключ}; неизвестные фигурные скобки остаются как есть (без KeyError)."""
    for key, value in values.items():
        template = template.replace("{" + key + "}", str(value))
    return template


class PlayerokBot:
    def __init__(self):
        self.account: Account | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.connected = False
        self.started_at = time.time()
        self.last_event_at: float | None = None
        self._stop = threading.Event()
        self._auth_failures = 0
        state.playerok_bot = self

    # ── запуск ──────────────────────────────────────────────────────────────
    async def run_bot(self) -> None:
        self.loop = asyncio.get_running_loop()
        api = Settings.get("config")["playerok"]["api"]
        if not api["token"]:
            logger.warning("Токен Playerok не задан — подключение пропущено. "
                           "Отправьте токен боту в Telegram: /token <токен>")
            return
        try:
            self.account = await asyncio.to_thread(self._connect, api)
        except UnauthorizedError:
            await self._notify_fatal("🔑 Playerok не принял токен/cookies (сессия истекла).\n"
                                     "Обновите: /token <токен> и /cookies <строка cookies>, затем /restart.")
            return
        except CloudflareDetectedException as e:
            await self._notify_fatal(f"🛡 {e.vendor_title} блокирует запросы. Обновите cookies "
                                     f"(/cookies), при необходимости смените прокси и User-Agent.")
            return
        self.connected = True
        logger.info(f"Авторизован на Playerok как {self.account.username}")
        threading.Thread(target=self._listen_loop, daemon=True, name="orca-listener").start()
        asyncio.create_task(self._health_loop())
        await call_bot_event("PLAYEROK_READY", [self])

    @staticmethod
    def _connect(api: dict) -> Account:
        return Account(
            token=api["token"], cookies=api.get("cookies", ""),
            user_agent=api["user_agent"], requests_timeout=api["requests_timeout"],
            proxy=api["proxy"] or None,
        ).get()

    def stop(self) -> None:
        self._stop.set()

    # ── слушатель (в отдельном потоке) ──────────────────────────────────────
    def _listen_loop(self) -> None:
        delay = Settings.get("config")["playerok"]["api"]["listener_requests_delay"]
        try:
            for event in EventListener(self.account).listen(requests_delay=delay):
                if self._stop.is_set():
                    break
                self.last_event_at = time.time()
                asyncio.run_coroutine_threadsafe(
                    call_playerok_event(event.type.name, [self, event]), self.loop)
        except Exception as e:
            logger.exception("Слушатель событий остановлен из-за ошибки")
            asyncio.run_coroutine_threadsafe(
                self._notify_fatal(f"❌ Слушатель Playerok остановлен: {e}\nПерезапустите: /restart"),
                self.loop)

    async def _health_loop(self) -> None:
        """Раз в 5 минут проверяет, что сессия жива; при протухшем токене сообщает в Telegram."""
        while not self._stop.is_set():
            await asyncio.sleep(HEALTH_CHECK_SECONDS)
            try:
                await asyncio.to_thread(self.account.get)
                self._auth_failures = 0
            except UnauthorizedError:
                self._auth_failures += 1
                if self._auth_failures == 2:  # дважды подряд — не случайный сбой
                    await self._notify_fatal("🔑 Сессия Playerok истекла (UNAUTHENTICATED). "
                                             "Обновите /token и /cookies, затем /restart.")
            except Exception as e:
                logger.debug(f"Проверка соединения не удалась: {e}")

    async def _notify_fatal(self, text: str) -> None:
        logger.error(text.replace("\n", " "))
        if state.telegram_bot:
            await state.telegram_bot.notify(text)

    # ── действия (блокирующие; вызывать из потока или через to_thread) ──────
    def send_message(self, chat_id: str, text: str | None = None,
                     photo_file_path: str | None = None):
        cfg = Settings.get("config")["playerok"]["watermark"]
        if text and cfg["enabled"] and cfg["value"]:
            text = f"{cfg['value']}\n{text}"
        return self.account.send_message(chat_id, text=text, photo_file_path=photo_file_path)

    def render(self, key: str, **values) -> str | None:
        """Текст автоответа из messages.json или None, если он выключен."""
        item = Settings.get("messages").get(key)
        if not item or not item.get("enabled") or not item.get("text"):
            return None
        return format_text("\n".join(item["text"]), **values)

    def buyer_name(self, deal, chat=None) -> str:
        me = getattr(self.account, "id", None)
        user = getattr(deal, "user", None)
        if user is not None and getattr(user, "id", None) != me:
            return user.username or "покупатель"
        for u in (getattr(chat, "users", None) or []):
            if u.id != me:
                return u.username or "покупатель"
        return "покупатель"
