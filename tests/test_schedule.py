import os
import sqlite3
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DATA = os.path.join(ROOT, "data")

from database import GroupRef, get_catalog, get_groups, get_user_group, init_db, save_schedule, find_groups  # noqa: E402
from parser import parse_all_schedules, parse_schedule  # noqa: E402
from routes import build_router  # noqa: E402
from schedule_parsers.common import load_config  # noqa: E402


@pytest.fixture(scope="module")
def records():
    return parse_all_schedules(DATA)


def test_each_format_is_detected_and_counted():
    config = load_config(DATA)
    counts = {
        "1 курс 1-6 группы.xlsx": 692,
        "1 курс 7-9 группы + ФОД.xlsx": 485,
        "schedule.xlsx": 919,
        "АФВ 1 курс - очная.xls": 229,
        "ЛФК 1 курс - очная.xls": 215,
        "ФР 1 курс - очная.xls": 430,  # два файла-подгруппы в одной таблице: Т2-07-26/1 и /2
        "Пед.образование 1 курс 2026 2 группы-2.xls": 468,
        "Реклама 1 курс 2026.xls": 235,
        "Режиссура 1 - очная.xls": 281,
        "Менеджмент 1 курс 2026 2 группы.xls": 472,
    }
    for name, expected in counts.items():
        assert len(parse_schedule(os.path.join(DATA, name), config)) == expected, name


def test_programs_and_same_numbers_do_not_collide(records):
    groups = {(r["program"], r["course"], r["group"]) for r in records}
    assert {p for p, _, _ in groups} == {
        "49.03.04", "49.03.01", "49.03.02", "44.03.01", "42.03.01", "51.03.05", "38.03.02",
    }
    assert {c for _, c, _ in groups} == {1}
    names = [g for _, _, g in groups]
    assert len(groups) >= len(set(names))  # одинаковые названия допустимы на разных направлениях


@pytest.fixture
def conn(tmp_path, records):
    c = init_db(str(tmp_path / "t.db"))
    save_schedule(c, records)
    return c


def test_catalog_and_find_groups(conn):
    catalog = get_catalog(conn)
    assert len(catalog) == 7
    some = get_groups(conn)[0]
    assert some in find_groups(conn, some.name)


def test_legacy_users_are_migrated(tmp_path, records):
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE users (telegram_id INTEGER PRIMARY KEY, group_name TEXT)")
    old.execute("CREATE TABLE lessons (id INTEGER PRIMARY KEY AUTOINCREMENT, week_sheet TEXT, day TEXT, date TEXT, time TEXT, group_name TEXT, subject TEXT)")
    old.execute("INSERT INTO users VALUES (1, 'Т1-04-26')")
    old.execute("INSERT INTO users VALUES (2, '3 группа')")
    old.commit()
    old.close()

    conn = init_db(path)  # добавляет колонки, ничего не теряя
    save_schedule(conn, records)
    assert get_user_group(conn, 1) == GroupRef("49.03.01", 1, "Т1-04-26")
    legacy = get_user_group(conn, 2)
    assert legacy.name == "3 группа" and legacy.program == "49.03.04"


def test_api_flow(conn):
    app = FastAPI()
    app.include_router(build_router(conn, ""))
    client = TestClient(app)
    data = client.get("/api/catalog?telegram_id=5").json()
    assert data["current"] is None and len(data["programs"]) == 7
    p = next(p for p in data["programs"] if p["code"] == "49.03.01")
    group = p["courses"][0]["groups"][0]
    r = client.post("/api/group", json={"telegram_id": 5, "group": group, "program": "49.03.01", "course": 1})
    assert r.status_code == 200
    assert client.get("/api/catalog?telegram_id=5").json()["current"]["program"] == "49.03.01"
    assert client.post("/api/group", json={"telegram_id": 5, "group": "нет", "program": "49.03.01", "course": 1}).status_code == 400


def test_bot_application_builds(monkeypatch):
    # ловит пропавшие обработчики и импорты: бот должен собираться при старте
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setenv("OWNER_ID", "1")
    import bot

    assert bot.build_application() is not None


def test_two_group_files_are_split_per_group(records):
    """В таблицах «2 группы» строки дисциплин идут парами; каждая группа получает свои пары."""
    by_group = {}
    for r in records:
        by_group.setdefault((r["program"], r["group"]), []).append(r)
    assert len(by_group[("38.03.02", "СГ1-5-26")]) == len(by_group[("38.03.02", "СГ1-6-26")]) == 236
    assert len(by_group[("44.03.01", "СГ2-1-26")]) == len(by_group[("44.03.01", "СГ2-2-26")]) == 234
    assert {g for p, g in by_group if p == "49.03.02"} == {"Т2-07-26/1", "Т2-07-26/2", "Т2-08-26", "Т2-09-26"}


def test_users_csv_backup():
    os.environ.setdefault("BOT_TOKEN", "123:abc")
    import bot

    data = bot.format_users_csv([(1, "ivan", "Иван", "4 группа", "2026-09-01", "2026-10-01", "49.03.04")]).decode("utf-8-sig")
    lines = data.strip().splitlines()
    assert lines[0].startswith("telegram_id,username")
    assert lines[1].startswith("1,ivan,Иван,4 группа")
