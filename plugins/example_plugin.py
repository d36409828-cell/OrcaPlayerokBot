"""Пример плагина. Скопируйте файл, переименуйте и меняйте под себя.

Что здесь показано:
  • META и init(ctx)
  • обработчик NEW_DEAL с приоритетом ДО встроенной автовыдачи (return True — забрать сделку себе)
  • обработчик NEW_MESSAGE (отвечает на «!пинг»)
  • собственная команда Telegram-панели через ctx.add_telegram_router

Плагин выключен по умолчанию логикой: он только отвечает на «!пинг» и пишет в лог,
сделки не трогает (возвращает None).
"""
META = {
    "name": "Пример плагина",
    "version": "1.0",
    "description": "Демонстрация API плагинов Orca",
    "author": "you",
}

_ctx = None


def init(ctx):
    global _ctx
    _ctx = ctx
    ctx.logger.info("Пример плагина подключён")

    try:  # своя команда в Telegram: /ping_plugin
        from aiogram import Router
        from aiogram.filters import Command
        from aiogram.types import Message

        router = Router()

        @router.message(Command("ping_plugin"))
        async def _ping(m: Message):
            await m.answer("pong from example plugin")

        ctx.add_telegram_router(router)
    except Exception as e:  # aiogram не обязателен для плагина
        ctx.logger.warning(f"Telegram-роутер не добавлен: {e}")


def on_new_deal(bot, event):
    deal = event.deal
    _ctx.logger.info(f"Новая сделка {deal.id}: {deal.item.name}")
    # Чтобы самому выдать товар и не пустить встроенную автовыдачу:
    #     bot.send_message(event.chat.id, "ваш товар ...")
    #     return True
    return None


def on_new_message(bot, event):
    if (event.message.text or "").strip().lower() == "!пинг":
        bot.send_message(event.chat.id, "pong 🏓")
        return True  # дальше (команды, приветствие) уже не нужно


PLAYEROK_EVENT_HANDLERS = {
    "NEW_DEAL": [on_new_deal],
    "NEW_MESSAGE": [on_new_message],
}
