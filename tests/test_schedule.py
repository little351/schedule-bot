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
        "49.03.04_1k_gr1-gr6.xlsx": 692,
        "49.03.04+49.03.01_1k_gr7-gr9+t1-04-26.xlsx": 485,
        "49.03.04_1k_gr10-gr16.xlsx": 919,
        "49.03.02_1k_t2-09-26.xls": 229,
        "49.03.02_1k_t2-08-26.xls": 215,
        "49.03.02_1k_t2-07-26.xls": 430,  # два файла-подгруппы в одной таблице: Т2-07-26/1 и /2
        "44.03.01_1k_sg2-1-26+sg2-2-26.xls": 468,
        "42.03.01_1k_sg1-3-26.xls": 235,
        "51.03.05_1k_sg2-4-26.xls": 281,
        "38.03.02_1k_sg1-5-26+sg1-6-26.xls": 472,
    }
    for name, expected in counts.items():
        assert len(parse_schedule(os.path.join(DATA, "1k", name), config)) == expected, name


SECOND_YEAR = {
    "38.03.02_2k_sg1-5-25+sg1-6-25+sg1-7-25.xls": 626,  # в одной паре у СГ1-6-25 две дисциплины: 627 пометок
    "42.03.01_2k_sg1-3-25.xls": 225,
    "44.03.01_2k_sg2-1-25.xls": 155,
    "49.03.02_2k_t2-07-25.xls": 386,
    "49.03.02_2k_t2-08-25.xls": 193,
    "49.03.02_2k_t2-09-25.xls": 193,
    "49.03.03_2k_t1-01-25.xls": 220,
    "49.03.04+49.03.01_2k_gr6-gr9+t1-04-25.xlsx": 614 + 153,  # «6 группа, Т1-04-25» - две группы с одним расписанием
    "49.03.04_2k_gr1-gr5.xlsx": 807,
    "49.03.04_2k_gr10-gr16.xlsx": 1404,
    "51.03.05_2k_sg2-4-25+sg2-5-25.xls": 558,
}


def test_second_year_files_are_read():
    config = load_config(DATA)
    for name, expected in SECOND_YEAR.items():
        assert len(parse_schedule(os.path.join(DATA, "2k", name), config)) == expected, name


def test_programs_and_same_numbers_do_not_collide(records):
    groups = {(r["program"], r["course"], r["group"]) for r in records}
    assert {p for p, _, _ in groups} == {
        "49.03.04", "49.03.01", "49.03.02", "44.03.01", "42.03.01", "51.03.05", "38.03.02", "49.03.03",
    }
    assert {c for _, c, _ in groups} == {1, 2}
    names = [g for _, _, g in groups]
    assert len(groups) >= len(set(names))  # одинаковые названия допустимы на разных направлениях


@pytest.fixture
def conn(tmp_path, records):
    c = init_db(str(tmp_path / "t.db"))
    save_schedule(c, records)
    return c


def test_catalog_and_find_groups(conn):
    catalog = get_catalog(conn)
    assert len(catalog) == 8
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
    assert data["current"] is None and len(data["programs"]) == 8
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
        if r["course"] == 1:
            by_group.setdefault((r["program"], r["group"]), []).append(r)
    assert len(by_group[("38.03.02", "СГ1-5-26")]) == len(by_group[("38.03.02", "СГ1-6-26")]) == 236
    assert len(by_group[("44.03.01", "СГ2-1-26")]) == len(by_group[("44.03.01", "СГ2-2-26")]) == 234
    first_year = {g for (c, p, g) in {(r["course"], r["program"], r["group"]) for r in records} if c == 1 and p == "49.03.02"}
    assert first_year == {"Т2-07-26/1", "Т2-07-26/2", "Т2-08-26", "Т2-09-26"}


def test_users_csv_backup():
    os.environ.setdefault("BOT_TOKEN", "123:abc")
    import bot

    data = bot.format_users_csv([(1, "ivan", "Иван", "4 группа", "2026-09-01", "2026-10-01", "49.03.04")]).decode("utf-8-sig")
    lines = data.strip().splitlines()
    assert lines[0].startswith("telegram_id,username")
    assert lines[1].startswith("1,ivan,Иван,4 группа")


def test_users_restore_from_users_command_text_and_csv(tmp_path):
    os.environ.setdefault("BOT_TOKEN", "123:abc")
    import bot

    rows = [
        (1, "ivan", "Иван", "4 группа", "2026-09-01T10:00:00+00:00", "2026-10-09T10:00:00+00:00", "49.03.04"),
        (2, None, "Мария", "Т1-04-26", None, "2026-10-08T10:00:00+00:00", "49.03.01"),
        (3, "old", None, "7 группа", None, None, None),  # старая запись без направления
        (4, "nogroup", "Пётр", None, None, "2026-10-01T10:00:00+00:00", None),
    ]
    text = "\n".join(bot.format_users_messages(rows))  # то, что бот присылает по /users
    from_text = bot.parse_users_backup(text)
    assert [r[0] for r in from_text] == [1, 2, 3, 4]
    assert from_text[1][3:7:3] == ("Т1-04-26", "49.03.01") and from_text[2][3] == "7 группа" and from_text[3][3] is None

    conn = init_db(str(tmp_path / "restore.db"))
    assert bot.restore_users(conn, from_text) == 4
    assert bot.restore_users(conn, from_text) == 0  # повторная загрузка ничего не дублирует и не затирает
    assert get_user_group(conn, 1) == GroupRef("49.03.04", 1, "4 группа")

    csv_rows = bot.parse_users_backup(bot.format_users_csv(rows).decode("utf-8-sig"))
    assert [r[0] for r in csv_rows] == [1, 2, 3, 4]


def test_files_are_found_in_course_folders_and_uploads_go_to_the_right_one(tmp_path):
    from schedule_parsers import list_schedule_files, upload_dir

    found = list_schedule_files(DATA)
    assert len(found) == 21

    root = str(tmp_path)
    assert upload_dir(root, "49.03.04_2k_gr1.xlsx", []) == os.path.join(root, "2k")
    # курса нет в имени: берём из самой таблицы; нет нигде или вне 1-5: корень data/
    assert upload_dir(root, "schedule.xlsx", [{"course": 3}, {"course": 4}]) == os.path.join(root, "3k")
    assert upload_dir(root, "schedule.xlsx", [{"course": 9}]) == root
    assert upload_dir(root, "schedule.xlsx", []) == root
