import os
import sqlite3
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DATA = os.path.join(ROOT, "data")

from database import (  # noqa: E402
    GroupRef, get_catalog, get_groups, get_user_group, init_db, save_schedule, find_groups, set_user_group,
)
from parser import parse_all_schedules, parse_schedule  # noqa: E402
from routes import build_router  # noqa: E402
from schedule_parsers.common import load_config  # noqa: E402


@pytest.fixture(scope="module")
def records():
    return parse_all_schedules(DATA)


def test_each_format_is_detected_and_counted():
    config = load_config(DATA)
    counts = {
        "49.03.04_1k_gr1-gr6.xlsx": 697,  # + общая лекция 01.09 в объединённой ячейке на 6 групп
        "49.03.04+49.03.01_1k_gr7-gr9+t1-04-26.xlsx": 488,
        "49.03.04_1k_gr10-gr16.xlsx": 925,
        "49.03.02_1k_t2-09-26.xls": 229,
        "49.03.02_1k_t2-08-26.xls": 215,
        "49.03.02_1k_t2-07-26.xls": 430,  # два файла-подгруппы в одной таблице: Т2-07-26/1 и /2
        "44.03.01_1k_sg2-1-26+sg2-2-26.xls": 468,
        "42.03.01_1k_sg1-3-26.xls": 235,
        "51.03.05_1k_sg2-4-26.xls": 281,
        "38.03.02_1k_sg1-5-26+sg1-6-26.xls": 472,
        "49.03.03_1k_t1-02-26.xls": 225,
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


THIRD_YEAR = {
    "38.03.02_3k_sg1-5-24+sg1-6-24.xls": 338,
    "42.03.01_3k_sg1-3-24.xls": 225,
    "44.03.01_3k_sg2-18-24.xls": 141,
    "49.03.02_3k_t2-07-24.xls": 175,
    "49.03.02_3k_t2-08-24.xls": 175,
    "49.03.02_3k_t2-09-24.xls": 175,
    "49.03.03_3k_t1-02-24.xls": 200,
    "49.03.04+49.03.01_3k_gr7-gr9+t1-04-24.xlsx": 712,  # «ФОД»: 7, 8, 9 группы и Т1-04-24
    "49.03.04_3k_gr1-gr5.xlsx": 868,
    "49.03.04_3k_gr10-gr16.xlsx": 1238,
    "51.03.05_3k_sg2-4-24.xls": 258,
}


def test_third_year_files_are_read():
    config = load_config(DATA)
    for name, expected in THIRD_YEAR.items():
        assert len(parse_schedule(os.path.join(DATA, "3k", name), config)) == expected, name


def test_programs_and_same_numbers_do_not_collide(records):
    groups = {(r["program"], r["course"], r["group"]) for r in records}
    assert {p for p, _, _ in groups} == {
        "49.03.04", "49.03.01", "49.03.02", "44.03.01", "42.03.01", "51.03.05", "38.03.02", "49.03.03",
    }
    assert {c for _, c, _ in groups} == {1, 2, 3}
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

    data = bot.format_users_csv([(1, "ivan", "Иван", "4 группа", "2026-09-01", "2026-10-01", "49.03.04", 2)]).decode("utf-8-sig")
    lines = data.strip().splitlines()
    assert lines[0].startswith("telegram_id,username")
    assert lines[0].endswith("program,course") and lines[1].startswith("1,ivan,Иван,4 группа")
    assert lines[1].endswith("49.03.04,2")


def test_users_restore_from_users_command_text_and_csv(tmp_path):
    os.environ.setdefault("BOT_TOKEN", "123:abc")
    import bot

    rows = [
        (1, "ivan", "Иван", "4 группа", "2026-09-01T10:00:00+00:00", "2026-10-09T10:00:00+00:00", "49.03.04", 1),
        (2, None, "Мария", "Т1-04-26", None, "2026-10-08T10:00:00+00:00", "49.03.01", 2),
        (3, "old", None, "7 группа", None, None, None, None),  # старая запись без направления
        (4, "nogroup", "Пётр", None, None, "2026-10-01T10:00:00+00:00", None, None),
    ]
    text = "\n".join(bot.format_users_messages(rows))  # то, что бот присылает по /users
    from_text = bot.parse_users_backup(text)
    assert [r[0] for r in from_text] == [1, 2, 3, 4]
    assert "Т1-04-26 (49.03.01, 2 курс)" in text
    assert from_text[1][3] == "Т1-04-26" and from_text[1][6:] == ("49.03.01", 2)
    assert from_text[2][3] == "7 группа" and from_text[3][3] is None

    conn = init_db(str(tmp_path / "restore.db"))
    assert bot.restore_users(conn, from_text) == 4
    assert bot.restore_users(conn, from_text) == 0  # повторная загрузка ничего не дублирует и не затирает
    assert get_user_group(conn, 1) == GroupRef("49.03.04", 1, "4 группа")
    assert get_user_group(conn, 2) == GroupRef("49.03.01", 2, "Т1-04-26")

    csv_rows = bot.parse_users_backup(bot.format_users_csv(rows).decode("utf-8-sig"))
    assert [r[0] for r in csv_rows] == [1, 2, 3, 4] and csv_rows[1][7] == 2


def test_users_restore_old_formats_without_course():
    os.environ.setdefault("BOT_TOKEN", "123:abc")
    import bot

    old_text = "Пользователей: 2\n@ivan · Иван · 4 группа (49.03.04) · 2026-10-09 · id 1\n(без username) · — · Т2-09-26 (49.03.02) · ? · id 2"
    rows = bot.parse_users_backup(old_text)
    assert [(r[0], r[3], r[6], r[7]) for r in rows] == [(1, "4 группа", "49.03.04", 1), (2, "Т2-09-26", "49.03.02", 1)]
    old_csv = "telegram_id,username,first_name,group_name,first_seen,last_seen,program\n1,ivan,Иван,4 группа,,2026-10-01,49.03.04\n"
    assert [(r[0], r[6], r[7]) for r in bot.parse_users_backup(old_csv)] == [(1, "49.03.04", 1)]


def test_user_course_is_saved_and_migrated(tmp_path):
    path = str(tmp_path / "m.db")
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE users (telegram_id INTEGER PRIMARY KEY, group_name TEXT, program TEXT)")
    old.execute("INSERT INTO users VALUES (5, '4 группа', '49.03.04')")
    old.commit()
    old.close()
    conn = init_db(path)
    assert conn.execute("SELECT course FROM users WHERE telegram_id = 5").fetchone() == (1,)
    set_user_group(conn, 6, GroupRef("49.03.02", 2, "Т2-09-25"))
    assert get_user_group(conn, 6) == GroupRef("49.03.02", 2, "Т2-09-25")


def test_files_are_found_in_course_folders_and_uploads_go_to_the_right_one(tmp_path):
    from schedule_parsers import list_schedule_files, upload_dir

    found = list_schedule_files(DATA)
    assert len(found) == 33

    root = str(tmp_path)
    assert upload_dir(root, "49.03.04_2k_gr1.xlsx", []) == os.path.join(root, "2k")
    # курса нет в имени: берём из самой таблицы; нет нигде или вне 1-5: корень data/
    assert upload_dir(root, "schedule.xlsx", [{"course": 3}, {"course": 4}]) == os.path.join(root, "3k")
    assert upload_dir(root, "schedule.xlsx", [{"course": 9}]) == root
    assert upload_dir(root, "schedule.xlsx", []) == root


def test_bot_picker_course_program_group_flow(tmp_path, records):
    os.environ.setdefault("BOT_TOKEN", "123:abc")
    import bot

    old_conn = bot.conn
    bot.conn = init_db(str(tmp_path / "picker.db"))
    save_schedule(bot.conn, records)
    try:
        def buttons(markup):
            return [b.callback_data for row in markup.inline_keyboard for b in row]

        text, markup = bot.picker_screen()
        assert buttons(markup) == ["c:1", "c:2", "c:3"]
        text, markup = bot.picker_screen("program", course=2)
        assert "p:2:49.03.04" in buttons(markup) and buttons(markup)[-1] == "b:c"
        text, markup = bot.picker_screen("groups", course=2, program="49.03.04")
        assert "g:49.03.04:2:1 группа" in buttons(markup) and buttons(markup)[-1] == "b:2"
        # на втором курсе «1 группа» и на первом тоже: это разные группы
        assert GroupRef("49.03.04", 1, "1 группа") in get_groups(bot.conn)
        assert GroupRef("49.03.04", 2, "1 группа") in get_groups(bot.conn)
        assert "2 курс" in GroupRef("49.03.04", 2, "1 группа").title
    finally:
        bot.conn = old_conn


def _subjects(records, course, group, week, needle):
    return [r["subject"] for r in records
            if (r["course"], r["group"], r["week_sheet"]) == (course, group, week) and needle in r["subject"]]


def test_merged_cell_lecture_reaches_every_group(records):
    # 01.09 общая лекция записана одной объединённой ячейкой D14:I14 на 6 групп
    for group in ("1 группа", "2 группа", "3 группа", "4 группа", "5 группа", "6 группа"):
        assert len(_subjects(records, 1, group, "01.09", "Использование электронной")) == 1, group


def test_matrix_rows_of_other_groups_keep_own_teacher_and_room(records):
    # строка группы под названием дисциплины может иметь своего преподавателя и аудиторию
    assert _subjects(records, 2, "СГ1-5-25", "05.10", "Элективные")[0].endswith("Попов Д.В. 27 ск")
    assert _subjects(records, 2, "СГ1-6-25", "05.10", "Элективные")[0].endswith("Маранян С.А. 27 ск")
    assert _subjects(records, 2, "СГ1-7-25", "05.10", "Элективные")[0].endswith("Махмудов М.М. 14 ск")
    assert "Шакина Е.Е." in _subjects(records, 2, "Т2-07-25/2", "05.10", "плавания")[0]
    assert "Никитина С.М." in _subjects(records, 2, "Т2-07-25/1", "05.10", "плавания")[0]
    assert "Фураев А.Н." in _subjects(records, 1, "Т2-07-26/2", "05.10", "Спортивная метрология")[0]
    # «5,6 гр. - Удалова, 7 гр - Ходырева»: каждой группе свой преподаватель
    assert "Удалова" in _subjects(records, 2, "СГ1-6-25", "05.10", "Иностранный язык")[0]
    assert "Удалова" not in _subjects(records, 2, "СГ1-7-25", "05.10", "Иностранный язык")[0]


def test_dates_without_a_schedule_week_are_not_filled_with_the_last_known_week(tmp_path, records):
    from datetime import datetime
    from database import get_week_sheet_for_date

    conn = init_db(str(tmp_path / "weeks.db"))
    save_schedule(conn, records)
    fod = GroupRef("49.03.04", 1, "1 группа")  # в файле расписание только до 26.10
    assert get_week_sheet_for_date(conn, fod, datetime(2026, 10, 28)) == "26.10"  # среда этой недели
    assert get_week_sheet_for_date(conn, fod, datetime(2026, 11, 1, 23, 59)) == "26.10"  # воскресенье
    assert get_week_sheet_for_date(conn, fod, datetime(2026, 11, 2)) is None
    assert get_week_sheet_for_date(conn, fod, datetime(2026, 8, 1)) is None  # раньше первой недели
    # окно практики посреди семестра: в таблице нет недель 12.10-09.11
    gap = GroupRef("44.03.01", 2, "СГ2-1-25")
    assert get_week_sheet_for_date(conn, gap, datetime(2026, 10, 20)) is None
    assert get_week_sheet_for_date(conn, gap, datetime(2026, 11, 17)) == "16.11"


def test_next_week_is_taken_by_date(tmp_path, records, monkeypatch):
    from datetime import datetime
    import database

    conn = init_db(str(tmp_path / "next.db"))
    save_schedule(conn, records)
    gap = GroupRef("44.03.01", 2, "СГ2-1-25")
    monkeypatch.setattr(database, "now_local", lambda: datetime(2026, 11, 10, 12, 0))
    assert database.get_current_week_sheet(conn, gap) is None
    assert database.get_relative_week_sheet(conn, gap, 1) == "16.11"  # следующая неделя - 16.11, а не «после текущей»
    monkeypatch.setattr(database, "now_local", lambda: datetime(2026, 12, 29, 12, 0))
    assert database.get_relative_week_sheet(conn, GroupRef("49.03.04", 2, "10 группа"), 1) is None


def test_today_is_moscow_time():
    from datetime import datetime, timedelta, timezone
    from database import now_local

    expected = datetime.now(timezone.utc) + timedelta(hours=3)
    assert abs((now_local() - expected.replace(tzinfo=None)).total_seconds()) < 5


def test_room_cell_with_several_rooms_is_resolved_for_the_lesson():
    from schedule_parsers.matrix_by_discipline import _room_for

    # по дням недели (0 - понедельник)
    assert _room_for("пн - 512 гл.к.     пт - 508 гл.к.", "п", 0, 1) == "512 гл.к."
    assert _room_for("пн - 512 гл.к.     пт - 508 гл.к.", "п", 4, 2) == "508 гл.к."
    assert _room_for("вт - 406 гл.к., ср,пт - 407 гл.к.", "п", 4, 1) == "407 гл.к."
    assert _room_for("151 мбк, пт - 154 мбк", "п", 4, 1) == "154 мбк"  # общая аудитория и исключение
    assert _room_for("151 мбк, пт - 154 мбк", "п", 1, 1) == "151 мбк"
    # по виду занятия и по номеру пары
    assert _room_for("л - 218 ауд.к. п - 327 мбк", "л", 2, 3) == "218 ауд.к."
    assert _room_for("л - 218 ауд.к. п - 327 мбк", "п", 2, 3) == "327 мбк"
    assert _room_for("2 п - 70 мбк, 3 п - 161 мбк", "п", 1, 3) == "161 мбк"
    # день недели не указан в ячейке или разбор неоднозначен: остаётся текст таблицы
    assert _room_for("пн - 512 гл.к. пт - 508 гл.к.", "п", 2, 1) == "пн - 512 гл.к. пт - 508 гл.к."
    assert _room_for("вт- 407 глк., ср. 2 п. - 209 гл.к., 3 п - 406 гл.к.", "п", 2, 2).startswith("вт- 407")
    assert _room_for("513 гл.к.", "п", 0, 1) == "513 гл.к."
    assert _room_for("411 л/а 205 ск", "п", 0, 1) == "411 л/а 205 ск"


def test_rooms_are_resolved_in_loaded_schedule(records):
    subject = _subjects(records, 1, "Т2-07-26/1", "05.10", "Иностранный язык")
    assert subject and "пн - " not in subject[0] and "пт - " not in subject[0]


def test_broadcast_delivery_skips_blocked_and_waits_on_rate_limit():
    import asyncio
    os.environ.setdefault("BOT_TOKEN", "123:abc")
    import bot
    from telegram.error import Forbidden, NetworkError, RetryAfter

    class FakeBot:
        def __init__(self):
            self.sent, self.calls = [], collections_counter()

        async def send_message(self, chat_id, text):
            self.calls[chat_id] += 1
            if chat_id == 2:
                raise Forbidden("Forbidden: bot was blocked by the user")
            if chat_id == 3 and self.calls[3] == 1:
                raise RetryAfter(5)
            if chat_id == 4:
                raise NetworkError("down")
            self.sent.append((chat_id, text))

    def collections_counter():
        import collections
        return collections.Counter()

    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    fake = FakeBot()
    sent, failed = asyncio.run(bot.deliver_broadcast(fake, [1, 2, 3, 4, 5], "Привет", pause=0, sleep=fake_sleep))
    assert (sent, failed) == (3, 2)  # 2 заблокировал бота, 4 недоступен после трёх попыток
    assert [c for c, _ in fake.sent] == [1, 3, 5]
    assert 6 in slept  # на RetryAfter(5) ждали 5+1 секунд
    assert fake.calls[4] == 3 and fake.calls[2] == 1


def test_broadcast_needs_confirmation_and_is_sent_once(tmp_path, monkeypatch):
    import asyncio
    os.environ.setdefault("BOT_TOKEN", "123:abc")
    import bot

    old_conn, old_owner = bot.conn, bot.OWNER_ID
    bot.conn = init_db(str(tmp_path / "bc.db"))
    bot.touch_user(bot.conn, 11, "a", "А")
    bot.touch_user(bot.conn, 12, "b", "Б")
    monkeypatch.setattr(bot, "OWNER_ID", "11")
    delivered = []

    async def fake_deliver(bot_, ids, text):
        delivered.append((list(ids), text))
        return len(ids), 0

    monkeypatch.setattr(bot, "deliver_broadcast", fake_deliver)

    class Msg:
        def __init__(self, text=""):
            self.text, self.out, self.chat_id = text, [], 11

        async def reply_text(self, text, reply_markup=None, **k):
            self.out.append((text, reply_markup))

    class Query:
        def __init__(self, data, owner=True):
            self.data, self.message, self.answers = data, Msg(), []

        async def answer(self, text=None, show_alert=False):
            self.answers.append(text)

        async def edit_message_text(self, text, **k):
            self.message.out.append((text, None))

    class App:
        tasks = []

        def create_task(self, coro):
            self.tasks.append(coro)

    class Bot:
        sent = []

        async def send_message(self, chat_id, text):
            self.sent.append((chat_id, text))

    context = type("C", (), {"bot_data": {}, "bot": Bot(), "application": App(), "args": []})()

    def upd(user_id, message=None, query=None):
        return type("U", (), {"effective_user": type("E", (), {"id": user_id})(), "message": message,
                              "callback_query": query})()

    try:
        # без текста рассылку не начинаем
        m = Msg("/broadcast")
        asyncio.run(bot.broadcast_command(upd(11, m), context))
        assert "broadcast" not in context.bot_data

        # с текстом: предпросмотр и кнопки, ничего не отправлено
        m = Msg("/broadcast Завтра пары по расписанию\nне меняются")
        asyncio.run(bot.broadcast_command(upd(11, m), context))
        assert "не меняются" in m.out[0][0] and delivered == []
        token = context.bot_data["broadcast"]["token"]
        assert "Получателей: 2" in m.out[1][0]

        # чужое нажатие игнорируется
        q = Query(f"bc:send:{token}")
        asyncio.run(bot.broadcast_button(upd(12, query=q), context))
        assert delivered == [] and "broadcast" in context.bot_data

        # отмена
        q = Query(f"bc:cancel:{token}")
        asyncio.run(bot.broadcast_button(upd(11, query=q), context))
        assert "broadcast" not in context.bot_data and delivered == []

        # подтверждение отправляет один раз, повторное нажатие ничего не делает
        asyncio.run(bot.broadcast_command(upd(11, Msg("/broadcast Текст")), context))
        token = context.bot_data["broadcast"]["token"]
        q = Query(f"bc:send:{token}")
        asyncio.run(bot.broadcast_button(upd(11, query=q), context))
        async def drain():
            await asyncio.gather(*context.application.tasks)

        asyncio.run(drain())
        asyncio.run(bot.broadcast_button(upd(11, query=Query(f"bc:send:{token}")), context))
        assert delivered == [([11, 12], "Текст")] or delivered == [([12, 11], "Текст")]
        assert len(delivered) == 1 and "Отправлено: 2" in context.bot.sent[-1][1]
    finally:
        bot.conn = old_conn
        bot.OWNER_ID = old_owner
