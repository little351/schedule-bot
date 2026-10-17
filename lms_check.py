"""Диагностика доступа к сайту вуза (Moodle) с сервера бота: показывает, на каком шаге останавливается вход.

Шаги: адрес (DNS) -> соединение (TCP) -> TLS -> страница входа -> вход по логину и паролю (только если
владелец попросил: /lms_check login). Логин и пароль берутся из LMS_LOGIN и LMS_PASSWORD, в отчёт не попадают:
пишутся только коды ответов, время и признаки (есть ли токен формы, капча, ссылка «Выход»).
"""
import os
import re
import socket
import ssl
import time
from urllib.parse import urlparse

import httpx

DEFAULT_BASE_URL = "https://lms.sportedu.ru"
LOGINTOKEN_RE = re.compile(r'name=["\']logintoken["\'][^>]*value=["\']([^"\']*)["\']|'
                           r'value=["\']([^"\']*)["\'][^>]*name=["\']logintoken["\']')
CAPTCHA_MARKERS = ("g-recaptcha", "recaptcha/api", "h-captcha", "hcaptcha")


def _timed(step, func, timeout_note=""):
    """(строка отчёта, результат или None). Ошибка сети не прерывает проверку, а попадает в отчёт."""
    started = time.monotonic()
    try:
        result = func()
        return f"✅ {step}: {time.monotonic() - started:.1f} с{timeout_note}", result
    except Exception as e:  # noqa: BLE001 - показываем тип ошибки владельцу
        return f"❌ {step}: {type(e).__name__} через {time.monotonic() - started:.1f} с", None


def run_checks(base_url=None, login=None, password=None, do_login=False, timeout=15.0, transport=None):
    """Строки отчёта. Первая неудача останавливает проверку: дальше идти бессмысленно."""
    base_url = (base_url or os.environ.get("LMS_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    parsed = urlparse(base_url)
    host, port = parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
    lines = [f"Сайт: {host}"]

    if transport is None:  # сетевые шаги по отдельности, чтобы понять, где именно обрывается
        line, addresses = _timed("адрес найден (DNS)", lambda: socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        lines.append(line)
        if addresses is None:
            return lines
        sock = {}

        def connect():
            sock["s"] = socket.create_connection((host, port), timeout=timeout)

        line, ok = _timed("соединение (TCP)", lambda: connect() or True)
        lines.append(line)
        if ok is None:
            return lines
        if parsed.scheme == "https":
            line, ok = _timed("шифрование (TLS)", lambda: ssl.create_default_context().wrap_socket(
                sock["s"], server_hostname=host).version())
            lines.append(line)
        sock["s"].close()
        if ok is None:
            return lines

    client = httpx.Client(follow_redirects=True, transport=transport, timeout=timeout,
                          headers={"User-Agent": "schedule-bot/1.0 (diagnostics)"})
    try:
        started = time.monotonic()
        try:
            page = client.get(f"{base_url}/login/index.php")
        except httpx.HTTPError as e:
            lines.append(f"❌ страница входа: {type(e).__name__} через {time.monotonic() - started:.1f} с "
                         "(соединение есть, но сайт не отдаёт страницу: похоже на блокировку по адресу сервера)")
            return lines
        lines.append(f"{'✅' if page.status_code < 400 else '❌'} страница входа: код {page.status_code}, "
                     f"{time.monotonic() - started:.1f} с")
        if page.status_code >= 400:
            return lines
        token = LOGINTOKEN_RE.search(page.text)
        lines.append(f"{'✅' if token else '⚠️'} токен формы (logintoken): {'есть' if token else 'не найден'}")
        captcha = any(m in page.text for m in CAPTCHA_MARKERS)
        lines.append(f"{'⚠️ на странице есть капча' if captcha else '✅ капчи на странице нет'}")
        lines.append("ℹ️ единый вход (SSO): " + ("возможно, есть кнопки внешнего входа" if "/auth/" in page.text else "не заметно"))

        if not do_login:
            lines.append("Вход не проверялся. Чтобы проверить логин и пароль, напишите /lms_check login")
            return lines
        if not login or not password:
            lines.append("❌ вход: не заданы LMS_LOGIN и LMS_PASSWORD в Render Environment")
            return lines
        data = {"username": login, "password": password, "anchor": ""}
        if token:
            data["logintoken"] = token.group(1) or token.group(2)
        started = time.monotonic()
        try:
            result = client.post(f"{base_url}/login/index.php", data=data)
        except httpx.HTTPError as e:
            lines.append(f"❌ вход: {type(e).__name__} через {time.monotonic() - started:.1f} с")
            return lines
        if "login/logout.php" in result.text:
            lines.append(f"✅ вход выполнен ({time.monotonic() - started:.1f} с)")
        elif "/login/" in str(result.url):
            lines.append(f"❌ вход: сайт вернул страницу входа (код {result.status_code}): логин или пароль не приняты, "
                         "либо нужна капча или другой способ входа")
        else:
            lines.append(f"⚠️ вход: код {result.status_code}, но ссылки «Выход» нет, подтвердить не удалось")
    finally:
        client.close()
    return lines
