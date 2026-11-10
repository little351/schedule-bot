"""Постоянная клавиатура под полем ввода: кнопки делают то же, что команды, и не мешают вводу группы и даты."""
import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "123:abc")


@pytest.fixture
def b(monkeypatch):
    import bot
    monkeypatch.setattr(bot, "OWNER_ID", "42")
    calls = []
    for name in ("week", "nweek", "weekdate", "group_command", "app_command", "stats_command", "mark_command",
                 "health_command"):
        async def fake(update, context, name=name):
            calls.append((name, list(context.args or [])))
        monkeypatch.setattr(bot, name, fake)
    monkeypatch.setattr(bot, "find_groups", lambda conn, text: [])
    return bot, calls


def make(user_id, text, chat_type="private"):
    sent = []

    async def reply_text(t, **kw):
        sent.append((t, kw.get("reply_markup")))

    message = SimpleNamespace(text=text, chat=SimpleNamespace(type=chat_type), reply_text=reply_text)
    update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=user_id),
                             effective_chat=message.chat)
    context = SimpleNamespace(args=None, user_data={}, bot_data={})
    return update, context, sent


def labels(markup):
    return [btn.text for row in markup.keyboard for btn in row]


def test_buttons_run_the_same_handlers(b):
    bot, calls = b
    for text, name in [(bot.KB_WEEK, "week"), (bot.KB_NWEEK, "nweek"), (bot.KB_GROUP, "group_command"),
                       (bot.KB_APP, "app_command")]:
        update, context, _ = make(7, text)
        asyncio.run(bot.handle_message(update, context))
        assert calls[-1] == (name, [])


def test_date_button_then_date_shows_that_week(b):
    bot, calls = b
    update, context, sent = make(7, bot.KB_DATE)
    asyncio.run(bot.handle_message(update, context))
    assert sent[-1][0] == bot.DATE_PROMPT and context.user_data["await_date"]
    update.message.text = "05.10"
    asyncio.run(bot.handle_message(update, context))
    assert calls[-1] == ("weekdate", ["05.10"])


def test_other_button_after_date_button_cancels_waiting(b):
    bot, calls = b
    update, context, _ = make(7, bot.KB_DATE)
    asyncio.run(bot.handle_message(update, context))
    update.message.text = bot.KB_WEEK
    asyncio.run(bot.handle_message(update, context))
    assert calls[-1][0] == "week" and "await_date" not in context.user_data


def test_owner_buttons_only_for_owner(b):
    bot, calls = b
    update, context, sent = make(7, bot.KB_STATS)
    asyncio.run(bot.handle_message(update, context))
    assert not calls and sent[-1][0].startswith("Не понял")  # чужому - как обычный текст
    assert bot.KB_STATS not in labels(sent[-1][1])
    for text, name in [(bot.KB_STATS, "stats_command"), (bot.KB_MARK, "mark_command"), (bot.KB_HEALTH, "health_command")]:
        update, context, _ = make(42, text)
        asyncio.run(bot.handle_message(update, context))
        assert calls[-1] == (name, [])


def test_start_shows_keyboard_in_private_chat_only(b):
    bot, _ = b
    update, context, sent = make(7, "/start")
    asyncio.run(bot.start(update, context))
    kb = sent[0][1]
    assert kb.is_persistent and kb.resize_keyboard and bot.KB_WEEK in labels(kb)
    update, context, sent = make(42, "/start")
    asyncio.run(bot.start(update, context))
    assert bot.KB_MARK in labels(sent[0][1])
    update, context, sent = make(7, "/start", chat_type="group")
    asyncio.run(bot.start(update, context))
    assert sent[0][1] is None


def test_group_number_text_still_works(b, monkeypatch):
    bot, _ = b
    group = bot.GroupRef("49.03.01", 1, "Т2-09-26")
    monkeypatch.setattr(bot, "find_groups", lambda conn, text: [group] if text == "Т2-09-26" else [])
    saved = []
    monkeypatch.setattr(bot, "set_user_group", lambda conn, uid, g: saved.append(g))
    update, context, sent = make(7, "Т2-09-26")
    asyncio.run(bot.handle_message(update, context))
    assert saved == [group] and sent[-1][0].startswith("Готово")


def test_keyboard_presses_are_counted(b, monkeypatch):
    bot, _ = b
    counted = []
    monkeypatch.setattr(bot.stats, "count", lambda m, n=1: counted.append(m))
    monkeypatch.setattr(bot.stats, "seen", lambda *a: None)
    update, _, _ = make(7, bot.KB_NWEEK)
    update.callback_query = None
    update.message.document = None
    bot.count_action(update)
    assert counted == ["kb.nweek"]


def test_owner_button_is_not_taken_as_teacher_password(b):
    bot, calls = b
    update, context, _ = make(42, bot.KB_STATS)
    context.bot_data["mark_password"] = "tok"
    asyncio.run(bot.handle_message(update, context))
    assert calls[-1][0] == "stats_command" and context.bot_data["mark_password"] == "tok"
