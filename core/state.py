"""Глобальные ссылки на живые объекты (чтобы модули не импортировали друг друга по кругу)."""
import asyncio

main_loop: asyncio.AbstractEventLoop | None = None
playerok_bot = None    # plbot.playerokbot.PlayerokBot
telegram_bot = None    # tgbot.telegrambot.TelegramBot
plugins: list = []     # core.plugins.Plugin
