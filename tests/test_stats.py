"""Статистика действий: счётчики в памяти, запись пачкой, отчёт /stats, события мини-аппа, сводка по понедельникам."""
import asyncio
import os
import sys
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("BOT_TOKEN", "123:abc")

import database  # noqa: E402
import stats  # noqa: E402
from database import get_sync_state, init_db, stats_read, stats_write  # noqa: E402
from routes import build_router  # noqa: E402
from test_notes import TOKEN, init_data  # noqa: E402


def fresh(tmp_path, name="s.db"):
    stats._counts.clear()
    stats._seen.clear()
    return init_db(str(tmp_path / name))


def days_ago(n, today=None):
    return ((today or database.now_local().date()) - timedelta(days=n)).isoformat()


def test_flush_writes_once_and_adds_up(tmp_path):
    conn = fresh(tmp_path)
    stats.count("cmd.week", 2)
    stats.seen(1, stats.SEEN_BOT)
    stats.seen(1, stats.SEEN_APP)
    stats.seen(2, stats.SEEN_APP)
    assert stats.flush(conn) > 0 and stats.pending() == 0
    assert stats.flush(conn) == 0  # нечего писать: в базу не ходим
    stats.count("cmd.week")
    stats.seen(1, stats.SEEN_GAME)
    stats.flush(conn)
    today = database.now_local().strftime("%Y-%m-%d")
    rows = dict(conn.execute("SELECT metric, value FROM stats_daily WHERE day = ?", (today,)).fetchall())
    assert rows["cmd.week"] == 3 and sum(v for m, v in rows.items() if m.startswith("hour.")) == 3
    kinds = dict(conn.execute("SELECT telegram_id, kinds FROM stats_seen").fetchall())
    assert kinds == {1: stats.SEEN_BOT | stats.SEEN_APP | stats.SEEN_GAME, 2: stats.SEEN_APP}


def test_failed_flush_keeps_counters(tmp_path, monkeypatch):
    conn = fresh(tmp_path)
    stats.count("cmd.app")
    stats.seen(5, stats.SEEN_BOT)

    def broken(*a):
        raise RuntimeError("база недоступна")
    monkeypatch.setattr(database, "stats_write", broken)
    try:
        stats.flush(conn)
    except RuntimeError:
        pass
    monkeypatch.undo()
    stats.flush(conn)
    assert conn.execute("SELECT value FROM stats_daily WHERE metric = 'cmd.app'").fetchone()[0] == 1


def test_report_people_retention_and_sections(tmp_path):
    conn = fresh(tmp_path)
    # неделей раньше были 1, 2, 3, 4; на этой неделе вернулись 1 и 2, пришёл новый 5
    seen = [(days_ago(10), uid, stats.SEEN_BOT) for uid in (1, 2, 3)] + [(days_ago(13), 4, stats.SEEN_BOT)]
    seen += [(days_ago(0), 1, stats.SEEN_APP | stats.SEEN_GAME), (days_ago(1), 2, stats.SEEN_BOT),
             (days_ago(0), 5, stats.SEEN_BOT | stats.SEEN_APP)]
    daily = [(days_ago(0), "cmd.week", 7), (days_ago(1), "cmd.week", 3), (days_ago(10), "cmd.week", 100),
             (days_ago(0), "open.menu", 4), (days_ago(0), "open.week", 1), (days_ago(0), "platform.android", 3),
             (days_ago(0), "platform.ios", 2), (days_ago(0), "hour.08", 9), (days_ago(0), "hour.13", 1),
             (days_ago(0), "game.played", 2), (days_ago(0), "game.points", 1500), (days_ago(0), "skin.neon", 1),
             (days_ago(0), "pick.start", 5), (days_ago(0), "pick.done", 4), (days_ago(0), "tab.other", 6)]
    stats_write(conn, daily, seen)
    data = stats.report_data(conn, 7)
    assert data["totals"]["cmd.week"] == 10  # 10 дней назад в недельный отчёт не входит
    assert data["uniq_7"] == 3 and data["uniq_30"] == 5 and data["players"] == 1
    assert data["retention"] == (4, 2)
    text = stats.format_report(data)
    assert "Сегодня 2 (бот 1, мини-апп 2) · вчера 1" in text
    assert "вернулись на этой неделе: 50%" in text
    assert "кнопка «Расписание» 4" in text and "Android 60%, iOS 40%" in text
    assert "/week 10" in text and "начат 5, выбрана группа 4" in text
    assert "Партий 2" in text and "игроков 1" in text and "очков 1 500" in text and "neon 1" in text
    assert "8–9 (90%)" in text
    month = stats.format_report(stats.report_data(conn, 30))
    assert "/week 110" in month
    csv_text = stats.format_csv(database.stats_daily_rows(conn, days_ago(29)), data["by_day"])
    assert csv_text.startswith("﻿день;люди;бот;мини-апп;игра;") and f"{days_ago(0)};2;1;2;1" in csv_text


def test_empty_report(tmp_path):
    conn = fresh(tmp_path)
    assert "данных пока нет" in stats.format_report(stats.report_data(conn, 7))


def test_compact_keeps_daily_people(tmp_path):
    conn = fresh(tmp_path)
    stats_write(conn, [], [(days_ago(200), 1, 3), (days_ago(200), 2, 1), (days_ago(1), 3, 1)])
    database.stats_compact(conn, days_ago(180))
    database.stats_compact(conn, days_ago(180))  # повтор ничего не удваивает
    assert conn.execute("SELECT COUNT(*) FROM stats_seen").fetchone()[0] == 1
    rows = dict(conn.execute("SELECT metric, value FROM stats_daily WHERE day = ?", (days_ago(200),)).fetchall())
    assert rows == {"uniq.all": 2, "uniq.bot": 2, "uniq.app": 1}


def test_mini_app_events(tmp_path):
    conn = fresh(tmp_path)
    app = FastAPI()
    app.include_router(build_router(conn, TOKEN))
    client = TestClient(app)
    me = init_data(7)
    assert client.post("/api/track", json={"init_data": "x", "events": {"tab.other": 1}}).status_code == 403
    r = client.post("/api/track", json={"init_data": me, "events": {"tab.other": 2, "day.swipe": 10_000,
                                                                       "заметка: купить хлеб": 1, "tab.schedule": -3}})
    assert r.json() == {"ok": True}
    client.post("/api/visit", json={"init_data": me, "src": "menu", "platform": "android"})
    client.post("/api/visit", json={"init_data": me})  # старый мини-апп без метки
    client.post("/api/game/score?game=tetris_hard", json={"score": 300}, headers={"X-Init-Data": me})
    client.post("/api/notes", json={"text": "сдать реферат"}, headers={"X-Init-Data": me})
    stats.flush(conn)
    rows = dict(conn.execute("SELECT metric, value FROM stats_daily").fetchall())
    assert rows["tab.other"] == 2 and rows["day.swipe"] == stats.APP_EVENT_MAX and "tab.schedule" not in rows
    assert not any("хлеб" in m or "реферат" in m for m in rows)  # тексты в статистику не попадают
    assert rows["open.menu"] == 1 and rows["open.other"] == 1 and rows["platform.android"] == 1
    assert rows["platform.other"] == 1
    assert rows["game.played"] == 1 and rows["game.played_hard"] == 1 and rows["game.points"] == 300
    assert rows["note.add"] == 1
    assert conn.execute("SELECT kinds FROM stats_seen WHERE telegram_id = 7").fetchone()[0] == \
        stats.SEEN_APP | stats.SEEN_GAME


def test_platforms_and_sources():
    assert [stats.app_platform(p) for p in ("android", "ios", "tdesktop", "weba", "", "unknown")] == \
        ["android", "ios", "desktop", "desktop", "other", "other"]
    assert stats.app_source("week") == "week" and stats.app_source("<script>") == "other"


def _update(text=None, data=None, user_id=3):
    user = SimpleNamespace(id=user_id, is_bot=False, username=None, full_name="Т")
    message = SimpleNamespace(text=text, document=None) if text is not None else None
    query = SimpleNamespace(data=data) if data is not None else None
    return SimpleNamespace(effective_user=user, message=message, callback_query=query)


def test_bot_counts_commands_buttons_and_ignores_service_updates(tmp_path, monkeypatch):
    import bot
    conn = fresh(tmp_path)
    monkeypatch.setattr(bot, "conn", conn)
    for upd in (_update("/week"), _update("/week@schedule_bot"), _update("/users"), _update("10"),
                _update(data="a:nweek"), _update(data="g:49.03.04:1:10 группа"),
                SimpleNamespace(effective_user=SimpleNamespace(id=9, is_bot=False), message=None, callback_query=None)):
        bot.count_action(upd)
    stats.flush(conn)
    rows = dict(conn.execute("SELECT metric, value FROM stats_daily").fetchall())
    assert rows["cmd.week"] == 2 and "cmd.users" not in rows and rows["msg.text"] == 1 and rows["btn.nweek"] == 1
    # блокировка бота (my_chat_member) не делает человека «активным»
    assert [r[0] for r in conn.execute("SELECT telegram_id FROM stats_seen").fetchall()] == [3]


def test_app_buttons_carry_source():
    import bot
    assert bot.actions_markup().inline_keyboard[0][0].web_app.url.endswith("?src=pick")
    assert bot.app_url("app").count("?") == 1


class FakeMessage:
    def __init__(self):
        self.texts, self.docs = [], []

    async def reply_text(self, text, **kw):
        self.texts.append(text)

    async def reply_document(self, document, filename, caption):
        self.docs.append((filename, document.read().decode("utf-8")))


def test_stats_command_report_and_csv(tmp_path, monkeypatch):
    import bot
    conn = fresh(tmp_path)
    monkeypatch.setattr(bot, "conn", conn)
    stats.count("cmd.week", 4)
    stats.seen(1, stats.SEEN_BOT)  # ещё не записано: /stats записывает перед отчётом
    msg = FakeMessage()
    asyncio.run(bot.stats_command(SimpleNamespace(message=msg), SimpleNamespace(args=[])))
    assert "📊 Статистика за 7 дн." in msg.texts[0] and "/week 4" in msg.texts[0]
    assert "Всего пользователей" in msg.texts[-1]  # прежняя статистика по курсам осталась
    asyncio.run(bot.stats_command(SimpleNamespace(message=msg), SimpleNamespace(args=["csv"])))
    name, body = msg.docs[0]
    assert name.endswith(".csv") and "cmd.week" in body


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text):
        self.sent.append(text)


def test_weekly_summary_once_per_week(tmp_path, monkeypatch):
    import bot
    conn = fresh(tmp_path)
    monkeypatch.setattr(bot, "conn", conn)
    monkeypatch.setattr(bot, "OWNER_ID", "42")
    monkeypatch.setattr(bot, "_weekly_done_for", None)
    monday = datetime(2026, 11, 16, 7, 0)
    monkeypatch.setattr(bot, "now_local", lambda: monday)
    stats_write(conn, [("2026-11-12", "cmd.week", 5), ("2026-11-16", "cmd.week", 1)], [("2026-11-12", 1, 1)])
    fake = FakeBot()
    asyncio.run(bot.weekly_stats_if_due(fake))
    asyncio.run(bot.weekly_stats_if_due(fake))
    assert len(fake.sent) == 1 and "Сводка за неделю 09.11–15.11" in fake.sent[0]
    assert "/week 5" in fake.sent[0]  # понедельник новой недели в прошлую сводку не входит
    assert get_sync_state(conn, "stats_weekly_sent") == "2026-11-16"
    monkeypatch.setattr(bot, "_weekly_done_for", None)  # после перезапуска сервера: отметка в базе
    asyncio.run(bot.weekly_stats_if_due(fake))
    assert len(fake.sent) == 1


def test_stats_read_numbers_are_ints(tmp_path):
    conn = fresh(tmp_path)
    data = stats_read(conn, days_ago(6), days_ago(0), days_ago(13), days_ago(6), days_ago(29))
    assert data["uniq_7"] == 0 and data["retention"] == (0, 0) and data["first_day"] is None
