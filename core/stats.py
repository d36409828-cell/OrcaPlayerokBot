"""Статистика продаж/возвратов/отзывов (формат совместим с bot_data/stats.json Seal-бота)."""
import datetime
import threading

import paths
from settings import get_json, set_json

_lock = threading.Lock()


def _default() -> dict:
    now = datetime.datetime.now()
    d = {"bot_launch_time": now.isoformat(), "month_started_at": now.isoformat(),
         "month_key": now.strftime("%Y-%m")}
    for scope in ("total", "month"):
        for k in ("sales", "reviews", "refund"):
            d[f"{k}_{scope}_count"] = 0
        for k in ("sales", "refund", "raises"):
            d[f"{k}_{scope}_sum"] = 0.0
    return d


def get() -> dict:
    with _lock:
        s = get_json(paths.STATS_FILE, _default(), True)
        key = datetime.datetime.now().strftime("%Y-%m")
        if s.get("month_key") != key:  # новый месяц — обнуляем месячные счётчики
            s["month_key"] = key
            s["month_started_at"] = datetime.datetime.now().isoformat()
            for k in list(s):
                if "_month_" in k:
                    s[k] = 0 if k.endswith("count") else 0.0
            set_json(paths.STATS_FILE, s)
        return s


def _bump(kind: str, amount: float | None = None) -> None:
    s = get()
    with _lock:
        for scope in ("total", "month"):
            s[f"{kind}_{scope}_count"] = s.get(f"{kind}_{scope}_count", 0) + 1
            if amount is not None:
                s[f"{kind}_{scope}_sum"] = round(s.get(f"{kind}_{scope}_sum", 0.0) + float(amount), 2)
        set_json(paths.STATS_FILE, s)


def add_sale(amount: float = 0.0) -> None: _bump("sales", amount)
def add_review() -> None: _bump("reviews")
def add_refund(amount: float = 0.0) -> None: _bump("refund", amount)
