"""Бот: правки сообщений, даты без года, даты в /users."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_users_list_shows_first_and_last_date_in_moscow_time():
    os.environ.setdefault("BOT_TOKEN", "123:abc")
    import bot
    rows = [(1, "ivan", "Иван", "4 группа", "2026-09-01T10:00:00+00:00", "2026-10-09T22:30:00+00:00", "49.03.04", 1),
            (2, None, "Мария", None, None, "2026-10-08T10:00:00+00:00", None, None),
            (3, None, None, None, None, None, None, None)]
    text = "\n".join(bot.format_users_messages(rows))
    assert "· 01.09.2026–10.10.2026 · id 1" in text  # 22:30 UTC 9 октября - уже 10 октября по Москве
    assert "· 08.10.2026 · id 2" in text and "· ? · id 3" in text
    restored = bot.parse_users_backup(text)
    assert [r[4:6] for r in restored] == [("2026-09-01", "2026-10-10"), ("2026-10-08", "2026-10-08"), (None, None)]


def test_date_without_year_is_the_nearest_one(monkeypatch):
    from datetime import datetime
    os.environ.setdefault("BOT_TOKEN", "123:abc")
    import bot
    monkeypatch.setattr(bot, "now_local", lambda: datetime(2026, 12, 20, 12, 0))
    assert bot.parse_input_date("10.01") == datetime(2027, 1, 10)
    assert bot.parse_input_date("05.12") == datetime(2026, 12, 5)
    monkeypatch.setattr(bot, "now_local", lambda: datetime(2027, 1, 10, 12, 0))
    assert bot.parse_input_date("25.12") == datetime(2026, 12, 25)
    assert bot.parse_input_date("05.10.2027") == datetime(2027, 10, 5)
    assert bot.parse_input_date("зз") is None


def test_edited_messages_are_ignored(monkeypatch):
    """Правка отправленной команды или номера группы не запускает обработчик повторно (раньше он падал)."""
    from telegram import Chat, Message, MessageEntity, Update, User
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    import bot
    monkeypatch.setattr(bot, "TOKEN", "123:abc")
    app = bot.build_application()
    me = User(123, "bot", True, username="schedbot")
    app.bot._bot_user = me
    user, chat = User(5, "Иван", False), Chat(5, "private")

    def update(text, edited):
        entities = [MessageEntity("bot_command", 0, len(text.split()[0]))] if text.startswith("/") else []
        msg = Message(1, datetime_now(), chat, from_user=user, text=text, entities=entities)
        msg.set_bot(app.bot)
        return Update(1, edited_message=msg) if edited else Update(1, message=msg)

    def handled(u):
        return [h.callback.__name__ for h in app.handlers[0] if h.check_update(u)]

    assert handled(update("/week", False)) == ["week"] and handled(update("/week", True)) == []
    assert handled(update("10", False)) == ["handle_message"] and handled(update("10", True)) == []


def test_long_owner_commands_do_not_hold_the_webhook(monkeypatch, tmp_path):
    """Загрузка файла и /lms_check уходят в фон: обработчик возвращается сразу, отчёт приходит потом."""
    import asyncio
    import types
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    import bot
    import lms_check
    monkeypatch.setattr(bot, "_is_owner", lambda update: True)
    monkeypatch.setattr(bot, "SCHEDULE_DIR", str(tmp_path))
    monkeypatch.setattr(lms_check, "run_checks", lambda **kw: ["ok"])
    replies, tasks = [], []

    async def reply_text(text):
        replies.append(text)

    async def get_file(file_id):
        async def download_to_drive(path):
            open(path, "wb").write(b"not a table")
        return types.SimpleNamespace(download_to_drive=download_to_drive)

    def update(document=None):
        message = types.SimpleNamespace(document=document, reply_text=reply_text)
        return types.SimpleNamespace(message=message)

    context = types.SimpleNamespace(args=[], bot=types.SimpleNamespace(get_file=get_file),
                                    application=types.SimpleNamespace(create_task=tasks.append))

    async def scenario():
        await bot.handle_document(update(types.SimpleNamespace(file_name="x.xlsx", file_id="f")), context)
        await bot.lms_check_command(update(), context)
        assert replies == ["Получил файл, обновляю расписание...", "Проверяю доступ к сайту вуза, это займёт до минуты..."]
        for task in tasks:
            await task
        assert replies[2].startswith("Ошибка при обработке файла") and replies[3] == "ok"
        assert os.listdir(tmp_path) == []  # временный файл убран

    asyncio.run(scenario())


def datetime_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)
