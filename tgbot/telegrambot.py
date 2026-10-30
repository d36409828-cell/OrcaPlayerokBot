"""Telegram-панель управления (aiogram 3)."""
import asyncio
import html
import time
from logging import getLogger

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core import state, stats
from core.handlers import call_bot_event
from core.proxy_utils import normalize_proxy
from core.utils import check_password, restart
from settings import Settings

logger = getLogger("orca.telegram")

TOGGLES = [
    ("playerok.auto_deliveries.enabled", "📤 Автовыдача"),
    ("playerok.auto_complete_deals.enabled", "✅ Авто-подтверждение"),
    ("playerok.first_message.enabled", "👋 Приветствие"),
    ("playerok.custom_commands.enabled", "🕹️ Команды в чате"),
    ("playerok.read_chat.enabled", "👀 Читать чаты"),
    ("playerok.watermark.enabled", "💧 Вотермарка"),
    ("playerok.tg_logging.enabled", "🔔 Уведомления"),
]
EVENT_TOGGLES = [
    ("new_user_message", "💬 Сообщения"), ("new_system_message", "⚙️ Системные"),
    ("new_deal", "🛒 Сделки"), ("new_review", "⭐ Отзывы"),
    ("new_problem", "🚨 Проблемы"), ("deal_status_changed", "🔄 Статусы"),
]
MAX_PASSWORD_ATTEMPTS = 5
BLOCK_SECONDS = 600


def _get(cfg: dict, path: str):
    for part in path.split("."):
        cfg = cfg[part]
    return cfg


def _set(cfg: dict, path: str, value) -> None:
    *parents, last = path.split(".")
    for part in parents:
        cfg = cfg[part]
    cfg[last] = value


def _mark(flag: bool) -> str:
    return "🟢" if flag else "🔴"


class AuthMiddleware(BaseMiddleware):
    """Пускает только авторизованных. Остальным — запрос пароля (с защитой от перебора)."""

    def __init__(self, tg: "TelegramBot"):
        self.tg = tg

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user and self.tg.is_signed(user.id):
            return await handler(event, data)
        if isinstance(event, Message):
            await self.tg.password_attempt(event)
        elif isinstance(event, CallbackQuery):
            await event.answer("Нет доступа", show_alert=True)
        return None


class TelegramBot:
    def __init__(self):
        cfg = Settings.get("config")["telegram"]
        self.bot = Bot(token=cfg["api"]["token"], session=self._make_session(cfg["api"]),
                       default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        self.dp = Dispatcher()
        self.router = Router()
        self._attempts: dict[int, list] = {}  # user_id -> [count, blocked_until]
        mw = AuthMiddleware(self)
        self.dp.message.outer_middleware(mw)
        self.dp.callback_query.outer_middleware(mw)
        self._register()
        self.dp.include_router(self.router)
        state.telegram_bot = self

    # ── подключение (напрямую / Cloudflare Worker / прокси) ─────────────────
    @staticmethod
    def _make_session(api: dict) -> AiohttpSession:
        mode = api.get("mode") or ("worker" if api.get("custom_api_url") else
                                   "proxy" if api.get("proxy") else "direct")
        if mode == "worker" and api.get("custom_api_url"):
            return AiohttpSession(api=TelegramAPIServer.from_base(api["custom_api_url"].rstrip("/")))
        if mode == "proxy" and api.get("proxy"):
            proxy = normalize_proxy(api["proxy"])
            if "://" not in proxy:
                proxy = "http://" + proxy
            return AiohttpSession(proxy=proxy)
        return AiohttpSession()

    # ── запуск ──────────────────────────────────────────────────────────────
    async def run_bot(self) -> None:
        for plugin in state.plugins:
            if plugin.ctx:
                for router in plugin.ctx.telegram_routers:
                    self.dp.include_router(router)
        asyncio.create_task(self._polling())
        await call_bot_event("TELEGRAM_READY", [self])

    async def _polling(self) -> None:
        while True:
            try:
                me = await self.bot.get_me()
                logger.info(f"Telegram бот запущен: @{me.username}")
                await self.dp.start_polling(self.bot, handle_signals=False,
                                            allowed_updates=["message", "callback_query"])
                return
            except Exception as e:
                logger.warning(f"Telegram недоступен ({type(e).__name__}: {e}). Повтор через 15 сек. "
                               f"Если Telegram заблокирован — настройте proxy или custom_api_url в config.json.")
                await asyncio.sleep(15)

    # ── авторизация ─────────────────────────────────────────────────────────
    @staticmethod
    def is_signed(user_id: int) -> bool:
        return user_id in Settings.get("config")["telegram"]["bot"]["signed_users"]

    async def password_attempt(self, message: Message) -> None:
        uid = message.from_user.id
        count, blocked_until = self._attempts.get(uid, [0, 0.0])
        if time.time() < blocked_until:
            return
        text = (message.text or "").strip()
        cfg = Settings.get("config")["telegram"]["bot"]
        if text and check_password(text, cfg["password_hash"]):
            Settings.update("config", lambda c: c["telegram"]["bot"]["signed_users"].append(uid)
                            if uid not in c["telegram"]["bot"]["signed_users"] else None)
            self._attempts.pop(uid, None)
            try:
                await message.delete()
            except Exception:
                pass
            await message.answer("✅ Доступ открыт. Меню — /menu, справка — /help")
            logger.info(f"Новый пользователь Telegram: {uid}")
            return
        count += 1
        if count >= MAX_PASSWORD_ATTEMPTS:
            self._attempts[uid] = [0, time.time() + BLOCK_SECONDS]
            await message.answer("⛔ Слишком много попыток. Подождите 10 минут.")
        else:
            self._attempts[uid] = [count, 0.0]
            await message.answer("🔒 Введите пароль доступа к боту.")

    # ── уведомления ─────────────────────────────────────────────────────────
    async def notify(self, text: str) -> None:
        for uid in Settings.get("config")["telegram"]["bot"]["signed_users"]:
            try:
                await self.bot.send_message(uid, text[:4000])
            except Exception as e:
                logger.warning(f"Не удалось отправить уведомление {uid}: {e}")

    # ── клавиатуры ──────────────────────────────────────────────────────────
    @staticmethod
    def kb_main() -> InlineKeyboardMarkup:
        b = InlineKeyboardBuilder()
        for text, data in (("📊 Статус", "m:status"), ("💰 Статистика", "m:stats"),
                           ("⚙️ Функции", "m:toggles"), ("🔔 Уведомления", "m:events"),
                           ("💬 Автоответы", "m:msgs"), ("📤 Автовыдача", "m:deliv"),
                           ("🔌 Плагины", "m:plugins")):
            b.button(text=text, callback_data=data)
        b.adjust(2)
        return b.as_markup()

    @staticmethod
    def kb_back(rows: list | None = None) -> InlineKeyboardMarkup:
        b = InlineKeyboardBuilder()
        for text, data in rows or []:
            b.button(text=text, callback_data=data)
        b.adjust(1)
        b.row(InlineKeyboardButton(text="⬅️ Меню", callback_data="m:main"))
        return b.as_markup()

    # ── содержимое экранов ──────────────────────────────────────────────────
    def screen(self, name: str) -> tuple[str, InlineKeyboardMarkup]:
        cfg = Settings.get("config")
        if name == "main":
            return "🐋 <b>Orca Playerok Bot</b>\nВыберите раздел:", self.kb_main()
        if name == "status":
            pb = state.playerok_bot
            lines = ["📊 <b>Статус</b>"]
            if pb and pb.connected:
                up = int(time.time() - pb.started_at)
                lines.append(f"Playerok: 🟢 {html.escape(str(pb.account.username))}")
                lines.append(f"Работает: {up // 3600} ч {up % 3600 // 60} мин")
                if pb.last_event_at:
                    lines.append(f"Последнее событие: {int(time.time() - pb.last_event_at)} сек назад")
            else:
                lines.append("Playerok: 🔴 не подключён (/token, /cookies, /restart)")
            active = [p for p in state.plugins if p.enabled and not p.error]
            lines.append(f"Плагины: {len(active)} из {len(state.plugins)}")
            return "\n".join(lines), self.kb_back()
        if name == "stats":
            s = stats.get()
            text = (f"💰 <b>Статистика</b>\n\n<b>За месяц</b>\nПродажи: {s['sales_month_count']} шт. "
                    f"на {s['sales_month_sum']} ₽\nВозвраты: {s['refund_month_count']} шт. "
                    f"на {s['refund_month_sum']} ₽\nОтзывы: {s['reviews_month_count']}\n\n"
                    f"<b>За всё время</b>\nПродажи: {s['sales_total_count']} шт. на {s['sales_total_sum']} ₽\n"
                    f"Возвраты: {s['refund_total_count']} шт. на {s['refund_total_sum']} ₽\n"
                    f"Отзывы: {s['reviews_total_count']}")
            return text, self.kb_back()
        if name == "toggles":
            rows = [(f"{_mark(_get(cfg, p))} {t}", f"t:{i}") for i, (p, t) in enumerate(TOGGLES)]
            return "⚙️ <b>Функции</b> (нажмите, чтобы переключить)", self.kb_back(rows)
        if name == "events":
            ev = cfg["playerok"]["tg_logging"]["events"]
            rows = [(f"{_mark(ev.get(k, True))} {t}", f"e:{i}") for i, (k, t) in enumerate(EVENT_TOGGLES)]
            return "🔔 <b>Какие события присылать</b>", self.kb_back(rows)
        if name == "msgs":
            msgs = Settings.get("messages")
            keys = list(msgs)
            rows = [(f"{_mark(msgs[k].get('enabled'))} {k}", f"mt:{i}") for i, k in enumerate(keys)]
            return ("💬 <b>Автоответы</b>\nВключение/выключение. Текст меняется командой:\n"
                    "<code>/setmsg ключ | строка 1 // строка 2</code>"), self.kb_back(rows)
        if name == "deliv":
            rules = Settings.get("auto_deliveries")
            if not rules:
                body = "Правил пока нет.\n"
            else:
                body = "\n".join(
                    f"• <b>{html.escape(r['name'])}</b> — ключи: {html.escape(', '.join(r['keywords']))}"
                    + (f" — товаров: {len(r.get('goods', []))}" if r.get("multi") else "")
                    for r in rules) + "\n"
            body += ("\n<code>/delivery_add Название | ключ1, ключ2 | текст выдачи</code>\n"
                     "В тексте {good} — выдаётся уникальный товар из списка (мульти-выдача).\n"
                     "<code>/goods_add Название | товар1 ;; товар2</code>\n"
                     "<code>/delivery_del Название</code>")
            return "📤 <b>Автовыдача</b>\n\n" + body, self.kb_back()
        if name == "plugins":
            if not state.plugins:
                return "🔌 Плагинов нет (папка plugins/).", self.kb_back()
            disabled = set(cfg["plugins"]["disabled"])
            lines = ["🔌 <b>Плагины</b> (переключение требует /restart)"]
            rows = []
            for p in state.plugins:
                err = f" ⚠️ {html.escape(p.error)}" if p.error else ""
                lines.append(f"• {html.escape(p.name)} v{html.escape(p.version)}{err}")
                rows.append((f"{_mark(p.id not in disabled)} {p.name}", f"pl:{p.id}"))
            return "\n".join(lines), self.kb_back(rows)
        return "?", self.kb_back()

    async def show(self, target: Message | CallbackQuery, name: str) -> None:
        text, kb = self.screen(name)
        if isinstance(target, CallbackQuery):
            try:
                await target.message.edit_text(text, reply_markup=kb)
            except TelegramBadRequest:
                pass  # «message is not modified»
            await target.answer()
        else:
            await target.answer(text, reply_markup=kb)

    # ── обработчики ─────────────────────────────────────────────────────────
    def _register(self) -> None:
        r = self.router

        @r.message(Command("start", "menu"))
        async def _menu(m: Message):
            await self.show(m, "main")

        @r.message(Command("help"))
        async def _help(m: Message):
            await m.answer(
                "<b>Команды</b>\n/menu — меню\n/status, /stats\n"
                "/delivery_add, /goods_add, /delivery_del — автовыдача\n"
                "/command_add <code>!кмд | текст</code>, /command_del <code>!кмд</code>\n"
                "/setmsg <code>ключ | текст</code> — автоответы\n"
                "/reply <code>chat_id | текст</code> — написать покупателю\n"
                "/token, /cookies — обновить доступ к Playerok\n/restart — перезапуск")

        @r.message(Command("status"))
        async def _status(m: Message):
            await self.show(m, "status")

        @r.message(Command("stats"))
        async def _stats(m: Message):
            await self.show(m, "stats")

        @r.callback_query(F.data.startswith("m:"))
        async def _screen(c: CallbackQuery):
            await self.show(c, c.data[2:])

        @r.callback_query(F.data.startswith("t:"))
        async def _toggle(c: CallbackQuery):
            path = TOGGLES[int(c.data[2:])][0]
            Settings.update("config", lambda cfg: _set(cfg, path, not _get(cfg, path)))
            await self.show(c, "toggles")

        @r.callback_query(F.data.startswith("e:"))
        async def _event_toggle(c: CallbackQuery):
            key = EVENT_TOGGLES[int(c.data[2:])][0]
            def fn(cfg):
                ev = cfg["playerok"]["tg_logging"]["events"]
                ev[key] = not ev.get(key, True)
            Settings.update("config", fn)
            await self.show(c, "events")

        @r.callback_query(F.data.startswith("mt:"))
        async def _msg_toggle(c: CallbackQuery):
            idx = int(c.data[3:])
            def fn(msgs):
                key = list(msgs)[idx]
                msgs[key]["enabled"] = not msgs[key].get("enabled")
            Settings.update("messages", fn)
            await self.show(c, "msgs")

        @r.callback_query(F.data.startswith("pl:"))
        async def _plugin_toggle(c: CallbackQuery):
            pid = c.data[3:]
            def fn(cfg):
                lst = cfg["plugins"]["disabled"]
                lst.remove(pid) if pid in lst else lst.append(pid)
            Settings.update("config", fn)
            await self.show(c, "plugins")

        # ---- автовыдача
        @r.message(Command("delivery_add"))
        async def _delivery_add(m: Message, command: CommandObject):
            parts = [p.strip() for p in (command.args or "").split("|", 2)]
            if len(parts) < 3 or not all(parts):
                return await m.answer("Формат: <code>/delivery_add Название | ключ1, ключ2 | текст выдачи</code>")
            name, keys, text = parts
            rule = {"name": name, "keywords": [k.strip() for k in keys.split(",") if k.strip()],
                    "message": text, "multi": "{good}" in text, "goods": []}
            def fn(rules):
                rules[:] = [x for x in rules if x["name"] != name] + [rule]
            Settings.update("auto_deliveries", fn)
            hint = "\nДобавьте товары: /goods_add" if rule["multi"] else ""
            await m.answer(f"✅ Правило «{html.escape(name)}» сохранено.{hint}")

        @r.message(Command("goods_add"))
        async def _goods_add(m: Message, command: CommandObject):
            name, _, goods = (command.args or "").partition("|")
            items = [g.strip() for g in goods.split(";;") if g.strip()]
            if not name.strip() or not items:
                return await m.answer("Формат: <code>/goods_add Название | товар1 ;; товар2</code>")
            found = []
            def fn(rules):
                for r_ in rules:
                    if r_["name"] == name.strip():
                        r_.setdefault("goods", []).extend(items)
                        r_["multi"] = True
                        found.append(len(r_["goods"]))
            Settings.update("auto_deliveries", fn)
            await m.answer(f"✅ Добавлено {len(items)}. Всего в наличии: {found[0]}" if found
                           else "❌ Правило не найдено.")
            try:
                await m.delete()  # товары — это секреты, не оставляем их в чате
            except Exception:
                pass

        @r.message(Command("delivery_del"))
        async def _delivery_del(m: Message, command: CommandObject):
            name = (command.args or "").strip()
            before = len(Settings.get("auto_deliveries"))
            Settings.update("auto_deliveries", lambda rules: rules.__setitem__(slice(None), [x for x in rules if x["name"] != name]))
            await m.answer("🗑 Удалено." if len(Settings.get("auto_deliveries")) < before else "❌ Не найдено.")

        # ---- команды и автоответы
        @r.message(Command("command_add"))
        async def _cmd_add(m: Message, command: CommandObject):
            cmd, _, text = (command.args or "").partition("|")
            cmd, text = cmd.strip().lower(), text.strip()
            if not cmd.startswith("!") or not text:
                return await m.answer("Формат: <code>/command_add !кмд | текст ответа</code>")
            Settings.update("custom_commands", lambda d: d.__setitem__(cmd, text))
            await m.answer(f"✅ Команда {html.escape(cmd)} сохранена.")

        @r.message(Command("command_del"))
        async def _cmd_del(m: Message, command: CommandObject):
            cmd = (command.args or "").strip().lower()
            Settings.update("custom_commands", lambda d: d.pop(cmd, None))
            await m.answer("🗑 Готово.")

        @r.message(Command("setmsg"))
        async def _setmsg(m: Message, command: CommandObject):
            key, _, text = (command.args or "").partition("|")
            key = key.strip()
            lines = [t.strip() for t in text.split("//") if t.strip()]
            if key not in Settings.get("messages") or not lines:
                keys = ", ".join(Settings.get("messages"))
                return await m.answer(f"Формат: <code>/setmsg ключ | строка 1 // строка 2</code>\nКлючи: {keys}")
            def fn(msgs):
                msgs[key]["text"] = lines
            Settings.update("messages", fn)
            await m.answer(f"✅ Текст «{key}» обновлён.")

        @r.message(Command("reply"))
        async def _reply(m: Message, command: CommandObject):
            chat_id, _, text = (command.args or "").partition("|")
            pb = state.playerok_bot
            if not (pb and pb.connected) or not chat_id.strip() or not text.strip():
                return await m.answer("Формат: <code>/reply chat_id | текст</code> (Playerok должен быть подключён)")
            try:
                await asyncio.to_thread(pb.send_message, chat_id.strip(), text.strip())
                await m.answer("✅ Отправлено.")
            except Exception as e:
                await m.answer(f"❌ {html.escape(str(e))}")

        # ---- доступ к Playerok
        async def _save_secret(m: Message, command: CommandObject, field: str, label: str):
            value = (command.args or "").strip()
            try:
                await m.delete()  # токен не должен висеть в истории чата
            except Exception:
                pass
            if not value:
                return await m.answer(f"Формат: <code>/{field if field != 'token' else 'token'} значение</code>")
            Settings.update("config", lambda c: c["playerok"]["api"].__setitem__(field, value))
            await m.answer(f"✅ {label} сохранён. Примените: /restart")

        @r.message(Command("token"))
        async def _token(m: Message, command: CommandObject):
            await _save_secret(m, command, "token", "Токен")

        @r.message(Command("cookies"))
        async def _cookies(m: Message, command: CommandObject):
            await _save_secret(m, command, "cookies", "Cookies")

        @r.message(Command("restart"))
        async def _restart(m: Message):
            await m.answer("🔄 Перезапускаюсь...")
            await asyncio.sleep(1)
            restart()
