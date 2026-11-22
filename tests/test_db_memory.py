"""Экономия базы (Neon считает время работы): частые чтения из памяти, частые записи пачкой, соединение
не держится открытым. Ошибка базы не ломает показ расписания и не теряет записи."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "123:abc")

import database  # noqa: E402
from database import (GroupRef, flush_users, get_sync_state, get_user_group, init_db, list_users,  # noqa: E402
                      pending_writes, restore_users, set_sync_state, set_user_group, touch_user, warm_cache)

GROUP = GroupRef("49.03.01", 1, "13 группа")


class Spy:
    """Считает запросы к таблицам users и sync_state и по желанию «роняет» их, как недоступная база."""

    def __init__(self, conn):
        self.conn, self.raw_execute, self.raw_many = conn, conn.execute, conn.executemany
        self.queries, self.down = [], False
        conn.execute, conn.executemany = self.execute, self.executemany

    def _check(self, sql):
        if "users" in sql or "sync_state" in sql:
            self.queries.append(sql)
            if self.down:
                raise RuntimeError("база недоступна")

    def execute(self, sql, params=()):
        self._check(sql)
        return self.raw_execute(sql, params)

    def executemany(self, sql, seq):
        self._check(sql)
        return self.raw_many(sql, seq)


@pytest.fixture
def conn(tmp_path):
    c = init_db(str(tmp_path / "m.db"))
    c.execute("INSERT INTO users (telegram_id, group_name, program, course) VALUES (1, '13 группа', '49.03.01', 1)")
    c.execute("INSERT INTO sync_state (key, value) VALUES ('last_run', '2026-11-21')")
    c.commit()
    return c


def test_frequent_reads_come_from_memory(conn):
    warm_cache(conn)
    spy = Spy(conn)
    for _ in range(50):
        assert get_user_group(conn, 1) == GROUP
        assert get_user_group(conn, 2) is None  # нет в базе - нет группы, без отдельного запроса
        assert get_sync_state(conn, "last_run") == "2026-11-21"
        assert get_sync_state(conn, "nothing", "d") == "d"
    assert spy.queries == []


def test_visits_are_written_in_one_batch(conn):
    warm_cache(conn)
    spy = Spy(conn)
    for _ in range(30):
        touch_user(conn, 1, "a", "А")
        touch_user(conn, 5, None, "Новый")
    assert spy.queries == [] and pending_writes(conn) == 2
    assert flush_users(conn) == 2
    assert len(spy.queries) == 1  # одна пачка на всех
    rows = {r[0]: r for r in list_users(conn)}
    assert rows[1][1] == "a" and rows[1][5] and rows[5][2] == "Новый" and rows[5][4]
    spy.queries.clear()
    assert flush_users(conn) == 0 and spy.queries == []  # нечего сбрасывать - базу не трогаем


def test_database_down_keeps_bot_working_and_nothing_is_lost(conn):
    warm_cache(conn)
    spy = Spy(conn)
    spy.down = True
    touch_user(conn, 7, "z", "Z")
    set_user_group(conn, 7, GROUP)  # не падает
    assert get_user_group(conn, 7) == GROUP and get_user_group(conn, 1) == GROUP
    with pytest.raises(RuntimeError):
        flush_users(conn)
    assert pending_writes(conn) == 2  # всё вернулось в память
    spy.down = False
    assert flush_users(conn) == 2
    row = conn.raw.execute("SELECT group_name, username FROM users WHERE telegram_id = 7").fetchone()
    assert row == ("13 группа", "z")


def test_first_seen_is_kept_and_group_is_not_touched_by_visits(conn):
    touch_user(conn, 1, "a", "А")
    flush_users(conn)
    first = conn.raw.execute("SELECT first_seen FROM users WHERE telegram_id = 1").fetchone()[0]
    touch_user(conn, 1, "b", "Б")
    flush_users(conn)
    row = conn.raw.execute("SELECT first_seen, username, group_name FROM users WHERE telegram_id = 1").fetchone()
    assert row == (first, "b", "13 группа")


def test_startup_without_database_loads_later(conn):
    spy = Spy(conn)
    spy.down = True
    warm_cache(conn)  # не падает
    spy.down = False
    assert get_user_group(conn, 1) == GROUP
    spy.queries.clear()
    assert get_user_group(conn, 1) == GROUP and spy.queries == []


def test_restore_and_sync_state_writes_update_memory(conn):
    warm_cache(conn)
    restore_users(conn, [(9, "r", "R", "13 группа", None, None, "49.03.01", 1)])
    assert get_user_group(conn, 9) == GROUP
    set_sync_state(conn, "last_ok", "1")
    assert get_sync_state(conn, "last_ok") == "1"


def test_postgres_connection_is_closed_after_each_use(monkeypatch):
    """Соединение не висит открытым между запросами (иначе Neon не засыпает); pg_session - одно на пачку."""
    events = []

    class FakeCursor:
        def execute(self, *a):
            events.append("query")

        def close(self):
            pass

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def close(self):
            events.append("close")

    monkeypatch.setattr(database, "_pg_schema_ready", True)
    monkeypatch.setattr(database, "_pg_open", lambda: events.append("open") or FakeConn())
    with database.pg_cursor() as cur:
        cur.execute("SELECT 1")
    assert events == ["open", "query", "close"]
    events.clear()
    with database.pg_session():
        for _ in range(3):
            with database.pg_cursor() as cur:
                cur.execute("SELECT 1")
    assert events == ["open", "query", "query", "query", "close"]


def test_old_mini_app_track_calls_still_answer(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import build_router
    app = FastAPI()
    app.include_router(build_router(init_db(str(tmp_path / "t.db")), "123:abc"))
    assert TestClient(app).post("/api/track", json={"init_data": "x", "events": {"tab.other": 1}}).json() == {"ok": True}
