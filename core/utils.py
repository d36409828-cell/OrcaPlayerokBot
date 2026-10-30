import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import sys
from logging.handlers import RotatingFileHandler

import paths


def setup_logger() -> None:
    paths.ensure_dirs()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    plain = "[%(asctime)s] %(levelname)s · %(name)s  %(message)s"
    datefmt = "%d.%m.%Y %H:%M:%S"
    try:
        import colorlog
        console_fmt = colorlog.ColoredFormatter(
            "%(log_color)s[%(asctime)s] %(levelname)s · %(name)s%(reset)s  %(message)s",
            datefmt=datefmt)
    except ImportError:
        console_fmt = logging.Formatter(plain, datefmt)
    console = logging.StreamHandler()
    console.setFormatter(console_fmt)
    root.addHandler(console)

    file_handler = RotatingFileHandler(paths.LATEST_LOG_FILE, maxBytes=5_000_000,
                                       backupCount=3, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(plain, datefmt))
    root.addHandler(file_handler)

    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def set_title(title: str) -> None:
    if os.name == "nt":
        os.system(f"title {title}")
    else:
        sys.stdout.write(f"\33]0;{title}\a")
        sys.stdout.flush()


def restart() -> None:
    """Перезапускает процесс бота."""
    logging.getLogger("orca").info("Перезапуск...")
    os.execv(sys.executable, [sys.executable] + sys.argv)


def mask(secret: str, keep: int = 4) -> str:
    secret = str(secret or "")
    return "" if not secret else secret[:keep] + "…" + f"({len(secret)} симв.)"


# ── пароль Telegram-панели: храним только sha256(соль + пароль) ─────────────
def _salt() -> str:
    paths.ensure_dirs()
    if not os.path.exists(paths.SALT_FILE):
        with open(paths.SALT_FILE, "w", encoding="utf-8") as f:
            f.write(secrets.token_hex(16))
    with open(paths.SALT_FILE, "r", encoding="utf-8") as f:
        return f.read().strip()


def hash_password(password: str) -> str:
    return hashlib.sha256((_salt() + password).encode("utf-8")).hexdigest()


def check_password(password: str, password_hash: str) -> bool:
    return bool(password_hash) and hmac.compare_digest(hash_password(password), password_hash)


async def maybe_await(result):
    if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
        return await result
    return result
