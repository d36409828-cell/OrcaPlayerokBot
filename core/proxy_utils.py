import re


def normalize_proxy(proxy: str) -> str:
    """Приводит прокси к виду, понятному библиотекам.

    Принимает: ip:port, user:pass@ip:port, ip:port:user:pass, socks5://..., http://...
    """
    proxy = (proxy or "").strip()
    if not proxy:
        return ""
    if re.match(r"^(socks5|socks4|http|https)://", proxy):
        return proxy
    parts = proxy.split(":")
    if "@" not in proxy and len(parts) == 4:  # ip:port:user:password
        ip, port, user, password = parts
        return f"{user}:{password}@{ip}:{port}"
    return proxy


def validate_proxy(proxy: str) -> bool:
    proxy = normalize_proxy(proxy)
    body = re.sub(r"^(socks5|socks4|http|https)://", "", proxy)
    body = body.split("@")[-1]
    return bool(re.match(r"^[\w.\-]+:\d{2,5}$", body))
