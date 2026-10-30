"""Встроенные обработчики событий Playerok (приоритет 100 — после плагинов)."""
import asyncio
import threading
import time
from logging import getLogger

from core import state, stats
from core.handlers import on_playerok_event
from data import Data
from settings import Settings

from playerokapi.enums import ChatTypes, ItemDealStatuses
from plbot.playerokbot import PlayerokBot, format_text

logger = getLogger("orca.handlers")
_goods_lock = threading.Lock()


# ── утилиты ─────────────────────────────────────────────────────────────────
def notify(event_key: str, text: str) -> None:
    """Уведомление в Telegram, если оно включено в настройках tg_logging."""
    cfg = Settings.get("config")["playerok"]["tg_logging"]
    if not cfg["enabled"] or not cfg["events"].get(event_key, True):
        return
    if state.telegram_bot and state.main_loop:
        asyncio.run_coroutine_threadsafe(state.telegram_bot.notify(text), state.main_loop)


def deal_values(bot: PlayerokBot, deal, chat=None, **extra) -> dict:
    return {
        "username": bot.buyer_name(deal, chat),
        "deal_id": deal.id,
        "deal_item_name": getattr(deal.item, "name", ""),
        "deal_amount": 1,  # TODO: количество в сделке библиотека пока не отдаёт отдельным полем
        "deal_link": f"https://playerok.com/deals/{deal.id}",
        **extra,
    }


def item_in_scope(cfg: dict, lists: dict, item_id: str) -> bool:
    """Режим «все предметы кроме excluded» или «только included»."""
    if cfg.get("all", True):
        return item_id not in lists.get("excluded", [])
    return item_id in lists.get("included", [])


def find_delivery(item_name: str) -> dict | None:
    name = (item_name or "").lower()
    for rule in Settings.get("auto_deliveries"):
        if any(k.strip().lower() in name for k in rule.get("keywords", []) if k.strip()):
            return rule
    return None


def take_good(rule_index_name: str) -> str | None:
    """Для мульти-выдачи: забирает первый товар из правила (потокобезопасно)."""
    with _goods_lock:
        rules = Settings.get("auto_deliveries")
        for rule in rules:
            if rule.get("name") == rule_index_name and rule.get("goods"):
                good = rule["goods"].pop(0)
                Settings.set("auto_deliveries", rules)
                return good
    return None


# ── сообщения ───────────────────────────────────────────────────────────────
@on_playerok_event("NEW_MESSAGE")
def on_new_message(bot: PlayerokBot, event):
    msg, chat = event.message, event.chat
    if msg.user is None or msg.user.id == bot.account.id:
        return  # своё или системное сообщение
    text = (msg.text or "").strip()
    notify("new_user_message", f"💬 <b>{msg.user.username}</b>:\n{text or '[вложение]'}\n\n🆔 чат: <code>{chat.id}</code>")
    if chat.type != ChatTypes.PM:
        return
    cfg = Settings.get("config")["playerok"]
    if cfg["read_chat"]["enabled"]:
        try:
            bot.account.mark_chat_as_read(chat.id)
        except Exception as e:
            logger.debug(f"mark_chat_as_read: {e}")

    # команды
    if cfg["custom_commands"]["enabled"] and text.startswith("!"):
        cmd = text.split()[0].lower()
        try:
            if cmd in ("!команды", "!commands"):
                reply = bot.render("cmd_commands")
            elif cmd in ("!продавец", "!seller"):
                reply = bot.render("cmd_seller")
                notify("new_user_message", f"🆘 <b>{msg.user.username}</b> зовёт продавца!\n🆔 чат: <code>{chat.id}</code>")
            else:
                custom = Settings.get("custom_commands").get(cmd)
                reply = format_text(custom, username=msg.user.username) if custom else None
            if reply:
                bot.send_message(chat.id, reply)
                return
        except Exception as e:
            err = bot.render("cmd_error", error=e)
            if err:
                bot.send_message(chat.id, err)
            return

    # приветствие
    fm = Settings.get("messages")["first_message"]
    if cfg["first_message"]["enabled"] and fm.get("enabled"):
        last = Data.greeted_at(msg.user.id)
        if last is None or time.time() - last > fm.get("cooldown_days", 7) * 86400:
            Data.mark_greeted(msg.user.id)
            bot.send_message(chat.id, format_text("\n".join(fm["text"]), username=msg.user.username))


# ── сделки ──────────────────────────────────────────────────────────────────
@on_playerok_event("NEW_DEAL")
def on_new_deal(bot: PlayerokBot, event):
    deal, chat = event.deal, event.chat
    if Data.is_deal_processed(deal.id, "new_deal"):
        return
    Data.mark_deal_processed(deal.id, "new_deal")
    price = getattr(deal.item, "price", 0) or 0
    stats.add_sale(price)
    vals = deal_values(bot, deal, chat)
    notify("new_deal", f"🛒 <b>Новая сделка</b>\n📦 {vals['deal_item_name']}\n👤 {vals['username']}\n"
                       f"💰 {price} ₽\n🔗 {vals['deal_link']}")

    greeting = bot.render("new_deal", **vals)
    if greeting:
        bot.send_message(chat.id, greeting)

    cfg = Settings.get("config")["playerok"]
    if not cfg["auto_deliveries"]["enabled"]:
        return
    rule = find_delivery(vals["deal_item_name"])
    if not rule:
        return
    deliver(bot, deal, chat, rule, vals)


def deliver(bot: PlayerokBot, deal, chat, rule: dict, vals: dict) -> bool:
    """Автовыдача по правилу. Возвращает True, если товар отправлен покупателю."""
    if Data.is_deal_processed(deal.id, "delivered"):
        return True
    good = None
    if rule.get("multi"):
        good = take_good(rule.get("name"))
        if good is None:
            notify("new_deal", f"⚠️ Товары для «{rule.get('name')}» закончились! "
                               f"Сделка {deal.id} ждёт ручной выдачи.")
            return False
    text = format_text(rule.get("message", ""), good=good or "", **vals)
    if not text.strip():
        return False
    try:
        bot.send_message(chat.id, text)
    except Exception as e:
        if good is not None:  # вернуть товар в очередь, чтобы не потерять
            with _goods_lock:
                rules = Settings.get("auto_deliveries")
                for r in rules:
                    if r.get("name") == rule.get("name"):
                        r.setdefault("goods", []).insert(0, good)
                Settings.set("auto_deliveries", rules)
        notify("new_deal", f"❌ Не удалось выдать товар по сделке {deal.id}: {e}")
        logger.error(f"Автовыдача по сделке {deal.id} не удалась: {e}")
        return False
    Data.mark_deal_processed(deal.id, "delivered")
    logger.info(f"Автовыдача: сделка {deal.id}, правило «{rule.get('name')}»")
    notify("new_deal", f"📤 Автовыдача выполнена: сделка {deal.id}")

    cfg = Settings.get("config")["playerok"]["auto_complete_deals"]
    if cfg["enabled"] and item_in_scope(cfg, Settings.get("auto_complete_items"), str(deal.item.id)):
        confirm_deal(bot, deal.id)
    return True


def confirm_deal(bot: PlayerokBot, deal_id: str) -> bool:
    """Помечает сделку «выполнена» (SENT) — покупателю приходит запрос подтвердить получение."""
    try:
        bot.account.update_deal(deal_id, ItemDealStatuses.SENT)
        return True
    except Exception as e:
        logger.error(f"Не удалось подтвердить сделку {deal_id}: {e}")
        notify("deal_status_changed", f"⚠️ Не удалось отметить сделку {deal_id} выполненной: {e}")
        return False


def _status_message(key: str, title: str, notify_key: str):
    def handler(bot: PlayerokBot, event):
        deal, chat = event.deal, event.chat
        stage = f"{key}"
        if Data.is_deal_processed(deal.id, stage):
            return
        Data.mark_deal_processed(deal.id, stage)
        vals = deal_values(bot, deal, chat)
        if key == "deal_refunded":
            stats.add_refund(getattr(deal.item, "price", 0) or 0)
        notify(notify_key, f"{title}\n📦 {vals['deal_item_name']}\n👤 {vals['username']}\n🔗 {vals['deal_link']}")
        reply = bot.render(key, **vals)
        if reply:
            bot.send_message(chat.id, reply)
    handler.__name__ = f"on_{key}"
    return handler


on_playerok_event("ITEM_SENT")(_status_message("deal_sent", "✅ Сделка выполнена продавцом", "deal_status_changed"))
on_playerok_event("DEAL_CONFIRMED")(_status_message("deal_confirmed", "🌟 Покупатель подтвердил сделку", "deal_status_changed"))
on_playerok_event("DEAL_CONFIRMED_AUTOMATICALLY")(_status_message("deal_confirmed", "🌟 Сделка подтверждена автоматически", "deal_status_changed"))
on_playerok_event("DEAL_ROLLED_BACK")(_status_message("deal_refunded", "📦 Возврат по сделке", "deal_status_changed"))
on_playerok_event("DEAL_HAS_PROBLEM")(_status_message("deal_has_problem", "🚨 Проблема в сделке!", "new_problem"))
on_playerok_event("DEAL_PROBLEM_RESOLVED")(_status_message("deal_problem_resolved", "✅ Проблема в сделке решена", "new_problem"))


@on_playerok_event("NEW_REVIEW")
def on_new_review(bot: PlayerokBot, event):
    deal, chat = event.deal, event.chat
    if Data.is_deal_processed(deal.id, "review"):
        return
    Data.mark_deal_processed(deal.id, "review")
    stats.add_review()
    vals = deal_values(bot, deal, chat)
    rating = getattr(getattr(deal, "review", None), "rating", "")
    notify("new_review", f"⭐ <b>Новый отзыв</b> {rating}\n📦 {vals['deal_item_name']}\n👤 {vals['username']}")
    reply = bot.render("new_review_response", **vals)
    if reply:
        bot.send_message(chat.id, reply)
