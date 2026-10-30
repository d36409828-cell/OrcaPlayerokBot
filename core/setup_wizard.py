"""Первичная настройка в консоли: токен Telegram-бота, пароль, (необязательно) токен Playerok."""
import re
import sys

from colorama import Fore

from core.proxy_utils import normalize_proxy, validate_proxy
from core.utils import hash_password
from settings import Settings

TG_TOKEN_RE = re.compile(r"^\d{7,12}:[A-Za-z0-9_-]{35}$")
COMMON_PASSWORDS = {"123456", "1234567", "12345678", "password", "qwerty", "admin", "123123", "111111"}


def _ask(prompt: str = "") -> str:
    if prompt:
        print(prompt)
    return input(f"  {Fore.WHITE}> {Fore.LIGHTWHITE_EX}").strip()


def _telegram_reachable(token: str, api: dict) -> bool:
    import requests
    base = (api.get("custom_api_url") or "https://api.telegram.org").rstrip("/")
    proxies = None
    if api.get("proxy"):
        p = normalize_proxy(api["proxy"])
        p = p if "://" in p else "http://" + p
        proxies = {"http": p, "https": p}
    try:
        r = requests.get(f"{base}/bot{token}/getMe", proxies=proxies, timeout=10)
        return r.status_code in (200, 401)
    except Exception:
        return False


def needs_setup() -> bool:
    cfg = Settings.get("config")["telegram"]
    return not cfg["api"]["token"] or not cfg["bot"]["password_hash"]


def run_wizard() -> None:
    if not sys.stdin.isatty():
        print("Требуется первичная настройка. Запустите бота вручную (start.bat / python bot.py) один раз.")
        sys.exit(1)
    cfg = Settings.get("config")

    while not cfg["telegram"]["api"]["token"]:
        token = _ask(f"\n{Fore.WHITE}Введите {Fore.CYAN}токен Telegram-бота{Fore.WHITE} (создаётся у @BotFather):")
        if not TG_TOKEN_RE.match(token):
            print(f"{Fore.LIGHTRED_EX}Токен не похож на настоящий, попробуйте ещё раз.")
            continue
        api = cfg["telegram"]["api"]
        while not _telegram_reachable(token, api):
            print(f"{Fore.YELLOW}Telegram недоступен с этого компьютера/сервера.")
            choice = _ask("Выберите: 1 — прокси, 2 — Cloudflare Worker, 3 — попробовать ещё раз, 4 — сохранить без проверки")
            if choice == "1":
                proxy = _ask("Прокси (ip:port, user:pass@ip:port или socks5://...):")
                if validate_proxy(proxy):
                    api["proxy"], api["custom_api_url"], api["mode"] = normalize_proxy(proxy), "", "proxy"
                else:
                    print(f"{Fore.LIGHTRED_EX}Неверный формат прокси.")
            elif choice == "2":
                url = _ask("Адрес воркера (https://имя.поддомен.workers.dev):")
                if url.startswith("https://"):
                    api["custom_api_url"], api["proxy"], api["mode"] = url.rstrip("/"), "", "worker"
            elif choice == "4":
                break
        api["token"] = token
        Settings.set("config", cfg)
        print(f"{Fore.GREEN}Токен Telegram сохранён.")

    while not cfg["telegram"]["bot"]["password_hash"]:
        pw = _ask(f"\n{Fore.WHITE}Придумайте {Fore.YELLOW}пароль доступа к боту{Fore.WHITE} (6–64 символа). "
                  f"Его нужно будет один раз отправить боту в Telegram:")
        if not (6 <= len(pw) <= 64) or pw.lower() in COMMON_PASSWORDS:
            print(f"{Fore.LIGHTRED_EX}Пароль слишком простой или неподходящей длины.")
            continue
        cfg["telegram"]["bot"]["password_hash"] = hash_password(pw)
        Settings.set("config", cfg)
        print(f"{Fore.GREEN}Пароль сохранён (в виде хеша).")

    if not cfg["playerok"]["api"]["token"]:
        print(f"\n{Fore.WHITE}Токен Playerok можно ввести сейчас или позже командой /token в Telegram. "
              f"Нажмите Enter, чтобы пропустить.")
        token = _ask("")
        if token:
            cfg["playerok"]["api"]["token"] = token
            cookies = _ask(f"{Fore.WHITE}Cookies (строка из браузера; Enter — пропустить):")
            cfg["playerok"]["api"]["cookies"] = cookies
            Settings.set("config", cfg)
