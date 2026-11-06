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


def test_errors_get_a_short_answer_and_one_owner_notice(monkeypatch):
    """Необработанная ошибка: пользователю короткий ответ, владельцу одно сообщение на одинаковые ошибки."""
    import asyncio
    from types import SimpleNamespace
    os.environ.setdefault("BOT_TOKEN", "123:abc")
    import bot
    monkeypatch.setattr(bot, "OWNER_ID", "42")
    monkeypatch.setattr(bot, "_error_notified", {})
    replies, owner = [], []

    async def reply_text(text):
        replies.append(text)

    async def send_message(chat_id, text):
        owner.append((chat_id, text))

    update = SimpleNamespace(effective_message=SimpleNamespace(chat=SimpleNamespace(type="private"), reply_text=reply_text),
                             effective_user=SimpleNamespace(id=7))
    context = SimpleNamespace(error=ValueError("сломалось"), bot=SimpleNamespace(send_message=send_message))
    asyncio.run(bot.on_error(update, context))
    asyncio.run(bot.on_error(update, context))
    assert replies == [bot.ERROR_TEXT] * 2
    assert len(owner) == 1 and owner[0][0] == 42 and "ValueError: сломалось" in owner[0][1]
    context.error = bot.Forbidden("blocked")  # бот заблокирован: молчим
    asyncio.run(bot.on_error(update, context))
    assert len(replies) == 2 and len(owner) == 1


def test_harmless_telegram_errors_are_not_sent_to_owner(monkeypatch):
    """TimedOut, «query is too old» и «message is not modified» только в лог; TimedOut пользователю ответ «повтори»."""
    import asyncio
    from types import SimpleNamespace
    os.environ.setdefault("BOT_TOKEN", "123:abc")
    import bot
    monkeypatch.setattr(bot, "OWNER_ID", "42")
    monkeypatch.setattr(bot, "_error_notified", {})
    replies, owner = [], []

    async def reply_text(text):
        replies.append(text)

    async def send_message(chat_id, text):
        owner.append(text)

    update = SimpleNamespace(effective_message=SimpleNamespace(chat=SimpleNamespace(type="private"), reply_text=reply_text),
                             effective_user=SimpleNamespace(id=7))
    context = SimpleNamespace(bot=SimpleNamespace(send_message=send_message))
    for error in (bot.BadRequest("Message is not modified: specified new message content and reply markup are "
                                 "exactly the same as a current content and reply markup of the message"),
                  bot.BadRequest("Query is too old and response timeout expired or query id is invalid")):
        context.error = error
        asyncio.run(bot.on_error(update, context))
    assert replies == [] and owner == []
    context.error = bot.TimedOut()
    asyncio.run(bot.on_error(update, context))
    assert replies == [bot.ERROR_TEXT] and owner == []
    context.error = bot.BadRequest("Chat not found")  # настоящая ошибка по-прежнему приходит владельцу
    asyncio.run(bot.on_error(update, context))
    assert len(owner) == 1 and "Chat not found" in owner[0]


def test_late_button_press_still_works():
    """Нажатие после сна сервера: answer() падает с «query is too old», но экран всё равно меняется."""
    import asyncio
    from types import SimpleNamespace
    os.environ.setdefault("BOT_TOKEN", "123:abc")
    import bot
    edits = []

    async def answer():
        raise bot.BadRequest("Query is too old and response timeout expired or query id is invalid")

    async def edit_message_text(text, reply_markup=None):
        edits.append(text)

    query = SimpleNamespace(data="l:b", answer=answer, edit_message_text=edit_message_text,
                            from_user=SimpleNamespace(id=7))
    asyncio.run(bot.on_pick(SimpleNamespace(callback_query=query), SimpleNamespace()))
    assert len(edits) == 1

    async def broken():
        raise bot.BadRequest("Chat not found")
    query.answer = broken
    try:
        asyncio.run(bot.on_pick(SimpleNamespace(callback_query=query), SimpleNamespace()))
    except bot.BadRequest:
        pass
    else:
        raise AssertionError("другие ошибки answer() не должны глотаться")


def test_menu_button_opens_mini_app_and_commands_stay(monkeypatch):
    """Кнопка «Меню» заменена на «Расписание» (мини-апп); список команд для «/» остался прежним."""
    import asyncio
    from types import SimpleNamespace
    from telegram import MenuButtonWebApp
    from telegram.error import NetworkError
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    import bot
    calls = {}

    class FakeBot:
        async def set_my_commands(self, commands):
            calls["commands"] = [c.command for c in commands]

        async def set_chat_menu_button(self, menu_button=None, chat_id=None):
            calls["menu"] = menu_button
            if calls.get("fail"):
                raise NetworkError("нет сети")

    app = SimpleNamespace(bot=FakeBot())
    asyncio.run(bot.post_init(app))
    assert calls["commands"] == ["start", "app", "group", "week", "nweek", "weekdate"]
    assert isinstance(calls["menu"], MenuButtonWebApp)
    assert calls["menu"].text == "Расписание" and calls["menu"].web_app.url == bot.WEBAPP_URL
    calls["fail"] = True
    asyncio.run(bot.post_init(app))  # сбой Telegram не мешает запуску бота


def test_week_reply_has_open_in_app_button_only_in_private_chat():
    from types import SimpleNamespace
    os.environ.setdefault("BOT_TOKEN", "123:abc")
    import bot
    markup = bot.open_app_markup(SimpleNamespace(effective_chat=SimpleNamespace(type="private")))
    button = markup.inline_keyboard[0][0]
    assert button.text == "📱 Открыть в приложении" and button.web_app.url == bot.WEBAPP_URL
    assert bot.open_app_markup(SimpleNamespace(effective_chat=SimpleNamespace(type="group"))) is None


def test_week_button_under_group_works(monkeypatch):
    """Кнопка «эта неделя» под выбранной группой: on_action подставляет свой update, расписание с кнопкой мини-аппа приходит."""
    import asyncio
    from types import SimpleNamespace
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    import bot
    sent = []

    async def reply_text(text, **kw):
        sent.append((text, kw))

    async def answer(*a, **kw):
        pass

    message = SimpleNamespace(chat=SimpleNamespace(type="private"), reply_text=reply_text)
    query = SimpleNamespace(data="a:week", message=message, from_user=SimpleNamespace(id=5), answer=answer)
    group = SimpleNamespace(title="1 группа")
    monkeypatch.setattr(bot, "get_user_group", lambda conn, uid: group)
    monkeypatch.setattr(bot, "get_current_week_sheet", lambda conn, g: "sheet")
    monkeypatch.setattr(bot, "get_week_schedule_grid", lambda conn, g, w: {})
    monkeypatch.setattr(bot, "format_week_text", lambda title, w, grid: "текст")
    asyncio.run(bot.on_action(SimpleNamespace(callback_query=query), SimpleNamespace(user_data={})))
    assert sent and sent[0][1]["reply_markup"].inline_keyboard[0][0].text == "📱 Открыть в приложении"
