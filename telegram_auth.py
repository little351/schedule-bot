import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

MAX_AGE_SECONDS = 24 * 60 * 60


def validate_init_data(init_data: str, bot_token: str, max_age: int = MAX_AGE_SECONDS):
    """Проверяет подпись initData мини-аппа (https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app)
    и возвращает данные пользователя: {"id", "username", "first_name", ...} или None, если подпись неверна."""
    if not init_data or not bot_token:
        return None
    params = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = params.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        return None

    try:
        if time.time() - int(params.get("auth_date", "0")) > max_age:
            return None
        user = json.loads(params["user"])
    except (ValueError, KeyError):
        return None
    return user if isinstance(user, dict) and "id" in user else None
