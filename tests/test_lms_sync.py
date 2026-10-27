import asyncio
import json
import os
import shutil
import types
import sys
from datetime import timedelta

import httpx
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DATA = os.path.join(ROOT, "data")
os.environ.setdefault("BOT_TOKEN", "123:abc")  # bot читает токен при первом импорте

import lms_sync  # noqa: E402
from database import init_db, list_synced_files, save_synced_file  # noqa: E402
from lms_sync import MoodleClient, find_resources, merged_records, run_sync  # noqa: E402

LOGIN, PASSWORD = "student42", "S3cret-pass"
FILE_A = os.path.join(DATA, "1k", "49.03.02_1k_t2-09-26.xls")
FILE_B = os.path.join(DATA, "1k", "49.03.02_1k_t2-08-26.xls")

LOGIN_PAGE = '<form id="login"><input type="hidden" name="logintoken" value="tok123"><input name="username"></form>'
COURSE_PAGE = """
<li><a href="https://lms.test/mod/resource/view.php?id=100"><span class="instancename">Расписание 1 курс Т2-09-26 <span class="accesshide "> Файл</span></span></a></li>
<li><a href="https://lms.test/mod/resource/view.php?id=101"><span class="instancename">Приказ о практике <span class="accesshide "> Файл</span></span></a></li>
<li><a href="https://lms.test/mod/forum/view.php?id=7">Форум</a></li>
"""


class FakeMoodle:
    """Подобие Moodle: форма входа с logintoken, страница курса, ресурс с перенаправлением на pluginfile."""

    def __init__(self, files=None, accept=True, login_html=LOGIN_PAGE):
        self.files = files if files is not None else {"100": ("t2-09-26.xls", open(FILE_A, "rb").read())}
        self.accept, self.login_html = accept, login_html
        self.requests = []
        self.logged_in = False

    def __call__(self, request):
        self.requests.append((request.method, request.url.path))
        path = request.url.path
        if path == "/login/index.php" and request.method == "GET":
            return httpx.Response(200, text=self.login_html)
        if path == "/login/index.php":
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            self.logged_in = self.accept and form.get("logintoken") == "tok123" \
                and form.get("username") == LOGIN and form.get("password") == PASSWORD
            if self.logged_in:
                return httpx.Response(200, text='<a href="/login/logout.php?sesskey=abc">Выход</a>')
            return httpx.Response(200, text=LOGIN_PAGE + '<div class="loginerrors">Неверный логин</div>')
        if not self.logged_in:
            return httpx.Response(200, text=LOGIN_PAGE)
        if path == "/course/view.php":
            return httpx.Response(200, text=COURSE_PAGE)
        if path == "/mod/resource/view.php":
            rid = request.url.params["id"]
            if rid in self.files:
                return httpx.Response(303, headers={"location": f"/pluginfile.php/1/mod_resource/content/1/{self.files[rid][0]}"})
            return httpx.Response(200, headers={"content-type": "text/html"}, text="<p>нет файла</p>")
        if path.startswith("/pluginfile.php/"):
            name = path.rsplit("/", 1)[1]
            for file_name, content in self.files.values():
                if file_name == name:
                    return httpx.Response(200, headers={"content-type": "application/vnd.ms-excel"}, content=content)
        return httpx.Response(404, text="нет")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LMS_LOGIN", LOGIN)
    monkeypatch.setenv("LMS_PASSWORD", PASSWORD)
    monkeypatch.setattr(lms_sync, "REQUEST_PAUSE", 0)
    schedule_dir = tmp_path / "data"
    schedule_dir.mkdir()
    shutil.copy(os.path.join(DATA, "programs.json"), schedule_dir)
    (schedule_dir / "lms_sources.json").write_text(json.dumps({
        "base_url": "https://lms.test",
        "pages": [{"url": "/course/view.php?id=5", "include": "расписание"}],
    }), encoding="utf-8")
    return str(schedule_dir), init_db(str(tmp_path / "t.db"))


def factory(site):
    return lambda base, login, password: MoodleClient(base, login, password, transport=httpx.MockTransport(site), pause=0)


def run(env, site, **kwargs):
    schedule_dir, conn = env
    return asyncio.run(run_sync(conn, schedule_dir, client_factory=factory(site), **kwargs))


def test_find_resources_uses_names_not_ids():
    found = find_resources(COURSE_PAGE, "https://lms.test/course/view.php?id=5")
    assert found == [("Расписание 1 курс Т2-09-26", "https://lms.test/mod/resource/view.php?id=100"),
                     ("Приказ о практике", "https://lms.test/mod/resource/view.php?id=101")]


def test_login_download_save_and_priority(env):
    schedule_dir, conn = env
    site = FakeMoodle()
    report = run(env, site)
    assert report.changed and not report.problems, report.text()
    assert "t2-09-26.xls: новый" in report.text()
    items = list_synced_files(conn)
    assert [i["file_name"] for i in items] == ["t2-09-26.xls"] and items[0]["records"] == 229
    # приказ не подошёл под фильтр, форум не файл: скачан один файл
    assert [p for m, p in site.requests if p == "/mod/resource/view.php"] == ["/mod/resource/view.php"]
    assert len(merged_records(conn, schedule_dir)) == 229  # без файлов в data/ расписание берётся из базы


def test_synced_file_overrides_the_same_group_from_disk(env):
    schedule_dir, conn = env
    os.makedirs(os.path.join(schedule_dir, "1k"))
    shutil.copy(FILE_A, os.path.join(schedule_dir, "1k"))
    assert len(merged_records(conn, schedule_dir)) == 229
    # в базе лежит файл с тем же набором групп: группы берутся из него, а не дублируются
    save_synced_file(conn, "x.xls", "x.xls", open(FILE_A, "rb").read(), "h", "2026-01-01T00:00:00", "{}", 1)
    assert len(merged_records(conn, schedule_dir)) == 229
    # битый файл в базе не ломает загрузку: остаются запасные источники
    save_synced_file(conn, "x.xls", "x.xls", b"garbage", "h2", "2026-01-02T00:00:00", "{}", 1)
    assert len(merged_records(conn, schedule_dir)) == 229


def test_second_run_is_unchanged_and_bad_version_is_rejected(env):
    schedule_dir, conn = env
    assert run(env, FakeMoodle()).changed
    again = run(env, FakeMoodle())
    assert not again.changed and "без изменений" in again.text()
    # вуз подменил файл другим: группа Т2-09-26 пропала
    broken = FakeMoodle(files={"100": ("t2-09-26.xls", open(FILE_B, "rb").read())})
    report = run(env, broken)
    assert not report.changed and "пропали группы" in report.text()
    assert report.problems
    assert list_synced_files(conn)[0]["records"] == 229  # осталась последняя хорошая версия


def test_unreadable_and_non_excel_files_do_not_replace_anything(env):
    schedule_dir, conn = env
    assert run(env, FakeMoodle()).changed
    junk = FakeMoodle(files={"100": ("t2-09-26.xls", b"<html>oops</html>")})
    report = run(env, junk)
    assert not report.changed and "не таблица Excel" in report.text()
    assert list_synced_files(conn)[0]["records"] == 229


def test_new_file_is_checked_against_what_the_bot_shows_now(env):
    schedule_dir, conn = env
    os.makedirs(os.path.join(schedule_dir, "1k"))
    shutil.copy(FILE_A, os.path.join(schedule_dir, "1k"))
    # тот же набор групп приходит новым файлом, но занятий в нём втрое меньше
    original = lms_sync.parse_bytes
    def shrunk(content, ext, sd):
        return original(content, ext, sd)[:60]
    lms_sync.parse_bytes = shrunk
    try:
        report = run(env, FakeMoodle())
    finally:
        lms_sync.parse_bytes = original
    assert not report.changed and "слишком резкое падение" in report.text()
    assert list_synced_files(conn) == []


def test_wrong_password_and_captcha_are_reported_without_secrets(env):
    report = run(env, FakeMoodle(accept=False))
    assert report.problems and "не принял логин" in report.text()
    assert PASSWORD not in report.text() and LOGIN not in report.text()
    captcha = run(env, FakeMoodle(login_html=LOGIN_PAGE + '<div class="g-recaptcha"></div>'))
    assert "капча" in captcha.text()


def test_scrub_hides_credentials(env):
    assert lms_sync.scrub(f"ошибка для {LOGIN} с {PASSWORD}") == "ошибка для *** с ***"


def test_site_down_is_a_calm_failure(env):
    def down(request):
        raise httpx.ConnectError("boom " + PASSWORD)
    report = asyncio.run(run_sync(env[1], env[0], client_factory=factory(down)))
    assert report.problems and "не удалось связаться" in report.text() and "/login/index.php" in report.text()
    assert PASSWORD not in report.text()


def test_timeout_names_the_request_and_retries_once(env):
    calls = []

    def silent(request):
        calls.append(request.url.path)
        raise httpx.ReadTimeout("slow")
    report = asyncio.run(run_sync(env[1], env[0], client_factory=factory(silent)))
    assert "не ответил на запрос /login/index.php" in report.text() and "ReadTimeout" in report.text()
    assert calls == ["/login/index.php", "/login/index.php"]


def test_not_configured_is_explained_not_failed(env, monkeypatch):
    monkeypatch.delenv("LMS_PASSWORD")
    report = run(env, FakeMoodle())
    assert not report.problems and "LMS_LOGIN и LMS_PASSWORD" in report.text()


def test_list_mode_shows_names_and_downloads_nothing(env):
    site = FakeMoodle()
    report = run(env, site, list_only=True)
    assert "Расписание 1 курс Т2-09-26" in report.text() and "Приказ о практике" in report.text()
    assert not [1 for _, p in site.requests if p == "/mod/resource/view.php"]
    assert list_synced_files(env[1]) == []


def test_new_file_name_supersedes_old_one_without_history(env):
    schedule_dir, conn = env
    run(env, FakeMoodle())
    renamed = FakeMoodle(files={"100": ("t2-09-26_v2.xls", open(FILE_A, "rb").read())})
    report = run(env, renamed)
    assert report.changed and "заменил t2-09-26.xls" in report.text()
    assert [i["file_name"] for i in list_synced_files(conn)] == ["t2-09-26_v2.xls"]


def test_is_due_follows_interval_and_failures(env):
    from database import set_sync_state
    from datetime import datetime, timezone
    conn = env[1]
    now = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
    assert lms_sync.is_due(conn, now)
    set_sync_state(conn, "last_run", (now - timedelta(hours=3)).isoformat())
    set_sync_state(conn, "last_ok", "1")
    assert not lms_sync.is_due(conn, now)
    set_sync_state(conn, "last_ok", "0")
    assert not lms_sync.is_due(conn, now)
    set_sync_state(conn, "last_run", (now - timedelta(hours=7)).isoformat())
    assert lms_sync.is_due(conn, now)
    set_sync_state(conn, "last_ok", "1")
    assert not lms_sync.is_due(conn, now)


def test_sync_command_reports_and_reloads_schedule(env, monkeypatch):
    import bot
    schedule_dir, conn = env
    monkeypatch.setattr(bot, "conn", conn)
    monkeypatch.setattr(bot, "SCHEDULE_DIR", schedule_dir)
    monkeypatch.setattr(lms_sync, "MoodleClient", lambda b, l, p: MoodleClient(b, l, p, transport=httpx.MockTransport(FakeMoodle()), pause=0))
    monkeypatch.setattr(lms_sync, "status", {"checked_at": None, "changed": {}})
    sent = []

    class Msg:
        async def reply_text(self, text, **kw):
            sent.append(text)

    class Update:
        message = Msg()

    class App:
        def create_task(self, coro):
            return asyncio.ensure_future(coro)

    class Ctx:
        args = []
        application = App()

    async def go():
        await bot.sync_command(Update(), Ctx())
        assert "Запускаю" in sent[0] and len(sent) == 1  # команда вернулась сразу, отчёт придёт позже
        while bot.sync_running or len(sent) == 1:
            await asyncio.sleep(0.01)

    asyncio.run(go())
    assert "t2-09-26.xls: новый" in sent[-1] and "Из подгрузки" in sent[-1]
    assert PASSWORD not in "".join(sent)
    assert lms_sync.get_sync_state(conn, "last_ok") == "1"
    assert lms_sync.status["checked_at"] and "checked_at" in lms_sync.get_sync_state(conn, "schedule_status")


def test_hung_page_is_skipped_and_the_rest_is_still_loaded(env):
    """Одна страница курса не открывается (сайт молчит): остальные подгружаются, в отчёте видно, какая пропущена."""
    schedule_dir, conn = env
    with open(os.path.join(schedule_dir, "lms_sources.json"), "w", encoding="utf-8") as f:
        json.dump({"base_url": "https://lms.test", "pages": [
            {"label": "зависшая", "url": "/course/view.php?id=6", "include": "расписание"},
            {"label": "рабочая", "url": "/course/view.php?id=5", "include": "расписание"}]}, f)

    class Hangs(FakeMoodle):
        def __call__(self, request):
            if request.url.path == "/course/view.php" and request.url.params["id"] == "6":
                raise httpx.ReadTimeout("slow")
            return super().__call__(request)

    report = run(env, Hangs())
    assert report.changed and "t2-09-26.xls: новый" in report.text()
    assert any("id=6" in p and "ReadTimeout" in p for p in report.problems)
    assert "память процесса" in report.text()


def test_run_stops_when_time_is_up(env, monkeypatch):
    monkeypatch.setenv("LMS_MAX_MINUTES", "1")
    ticks = iter([0, 0, 1000, 2000, 3000, 4000, 5000, 6000])
    fake_time = types.SimpleNamespace(monotonic=lambda: next(ticks, 9999), sleep=lambda _: None)
    monkeypatch.setattr(lms_sync, "time", fake_time)  # подменяем только в модуле подгрузки, не во всём процессе
    report = run(env, FakeMoodle())
    assert any("вышел срок" in p for p in report.problems)


def test_files_left_over_from_old_runs_are_not_shown(env):
    """Файл, которого уже нет на страницах курсов (например, от прошлого обхода сайта), в расписание не попадает,
    хотя в базе остаётся."""
    schedule_dir, conn = env
    run(env, FakeMoodle())
    lms_sync.save_synced_file(conn, "старый магистерский.xls", "старый магистерский.xls", b"x", "sha", "2026-10-12T00:00:00+00:00", "{}", 1)
    assert "старый магистерский.xls" in {i["key"] for i in list_synced_files(conn)}
    assert "старый магистерский.xls" not in lms_sync.active_keys(conn)
    assert lms_sync.active_keys(conn)


def test_action_buttons_call_the_command_handlers(env, monkeypatch):
    import bot
    schedule_dir, conn = env
    monkeypatch.setattr(bot, "conn", conn)
    sent = []

    class Msg:
        async def reply_text(self, text, **kw):
            sent.append(text)

    class Query:
        data = "a:week"
        message = Msg()
        from_user = type("U", (), {"id": 123})()

        async def answer(self):
            pass

    class Update:
        callback_query = Query()

    class Ctx:
        user_data = {}

    asyncio.run(bot.on_action(Update(), Ctx()))
    assert sent == [bot.NO_GROUP_MSG]  # группа не выбрана: тот же ответ, что у /week
    Query.data = "a:date"
    asyncio.run(bot.on_action(Update(), Ctx()))
    assert "дату" in sent[-1]
    labels = [b.text for row in bot.actions_markup().inline_keyboard for b in row]
    assert len(labels) == 5 and "Сегодня" not in labels and bot.actions_markup().inline_keyboard[0][0].web_app


def test_two_files_for_the_same_groups_both_stay(env):
    """Основное расписание и отдельный файл (например, ФОД) для тех же групп не вытесняют друг друга."""
    schedule_dir, conn = env
    content = open(FILE_A, "rb").read()

    class TwoFiles(FakeMoodle):
        def __call__(self, request):
            if request.url.path == "/course/view.php" and self.logged_in:
                return httpx.Response(200, text=COURSE_PAGE + '<li><a href="https://lms.test/mod/resource/view.php?id=102">'
                                      'Расписание ФОД Т2-09-26</a></li>')
            return super().__call__(request)

    site = TwoFiles(files={"100": ("t2-09-26.xls", content), "102": ("fod-t2-09-26.xls", content)})
    report = run(env, site)
    assert "заменил" not in report.text()
    assert sorted(i["key"] for i in list_synced_files(conn)) == ["fod-t2-09-26.xls", "t2-09-26.xls"]
    assert lms_sync.active_keys(conn) == {"fod-t2-09-26.xls", "t2-09-26.xls"}
    run(env, site)  # повторный запуск ничего не удаляет
    assert len(list_synced_files(conn)) == 2


def test_source_summary_lists_only_used_files_in_moscow_time(env):
    schedule_dir, conn = env
    run(env, FakeMoodle())
    lms_sync.save_synced_file(conn, "старый.xls", "старый.xls", b"x", "sha", "2026-10-12T21:30:00+00:00", "{}", 1)
    text = lms_sync.source_summary(conn)
    assert "t2-09-26.xls" in text and "старый.xls" not in text and "МСК" in text and "UTC" not in text
    assert lms_sync._moscow_time("2026-10-12T21:30:00+00:00") == "13.10.2026 00:30 МСК"


def test_start_reads_ready_schedule_not_files(env, monkeypatch):
    """Старт бота не качает сами файлы из базы (это трафик Neon), а берёт готовое сжатое расписание."""
    schedule_dir, conn = env
    assert run(env, FakeMoodle()).changed
    expected = merged_records(conn, schedule_dir)
    assert len(expected) == 229

    def no_content(*args):
        raise AssertionError("содержимое файла не должно скачиваться при старте")
    monkeypatch.setattr(lms_sync, "get_synced_content", no_content)
    assert merged_records(conn, schedule_dir) == expected


def as_saved(records):
    """Как записи попадают в таблицу lessons: дата строкой."""
    return [{**r, "date": str(r["date"])} for r in records]


def test_old_rows_and_changed_parsers_are_reparsed_once(env, monkeypatch):
    schedule_dir, conn = env
    assert run(env, FakeMoodle()).changed
    expected = as_saved(merged_records(conn, schedule_dir))
    calls = []
    real = lms_sync.get_synced_content
    monkeypatch.setattr(lms_sync, "get_synced_content", lambda c, key: calls.append(key) or real(c, key))
    # строка без готового расписания (сохранена до этой версии бота): файл разбирается один раз
    conn.execute("UPDATE synced_files SET parsed = NULL, parsed_version = NULL")
    conn.commit()
    assert as_saved(merged_records(conn, schedule_dir)) == expected
    assert as_saved(merged_records(conn, schedule_dir)) == expected
    assert calls == ["t2-09-26.xls"]
    # поменялись парсеры или programs.json: тоже разбирается заново
    monkeypatch.setattr(lms_sync, "parsed_version", lambda d: "другая")
    assert as_saved(merged_records(conn, schedule_dir)) == expected
    assert calls == ["t2-09-26.xls", "t2-09-26.xls"]


def test_changed_days_ignore_new_weeks_and_past_days(env, monkeypatch):
    """Изменением считаются только уже известные дни с сегодняшнего: новые недели и прошлое не в счёт."""
    from datetime import datetime, timezone
    conn = env[1]
    monkeypatch.setattr(lms_sync, "status", {"checked_at": None, "changed": {}})

    def lesson(date, time, subject, group="1 группа"):
        return {"program": "49.03.04", "course": 1, "group": group, "date": date, "time": time, "subject": subject}

    old = [lesson("2026-10-20", "8:50", "Философия"), lesson("2026-10-27", "8:50", "История"),
           lesson("2026-10-28", "10:40", "Анатомия"), lesson("2026-10-28", "8:50", "Анатомия", "2 группа")]
    new = [lesson("2026-10-20", "8:50", "Перенесено"), lesson("2026-10-27", "8:50", "История"),
           lesson("2026-10-28", "12:40", "Анатомия"), lesson("2026-11-03", "8:50", "Новая неделя"),
           lesson("2026-10-28", "8:50", "Анатомия", "2 группа")]
    today = "2026-10-26"
    changes = lms_sync.changed_days(lms_sync.day_fingerprints(old, today), lms_sync.day_fingerprints(new, today), today)
    assert changes == {"49.03.04|1|1 группа": ["2026-10-28"]}
    # порядок пар в файле не важен
    assert lms_sync.changed_days(lms_sync.day_fingerprints(old, today),
                                 lms_sync.day_fingerprints(list(reversed(old)), today), today) == {}

    now = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
    lms_sync.note_changes(conn, changes, now)
    lms_sync.note_changes(conn, {"49.03.04|1|2 группа": ["2026-10-29"]}, now + timedelta(days=15))
    assert list(lms_sync.status["changed"]) == ["49.03.04|1|2 группа"]  # старое забыто через 14 дней
    lms_sync.status["changed"] = {}
    lms_sync.load_status(conn)  # после перезапуска берётся из базы
    assert lms_sync.status["changed"]["49.03.04|1|2 группа"]["dates"] == ["2026-10-29"]


def test_day_answer_carries_update_date_and_group_changes(monkeypatch):
    import routes
    from database import GroupRef
    monkeypatch.setattr(lms_sync, "status", {"checked_at": "2026-10-26T22:30:00+00:00",
                                             "changed": {"49.03.04|1|1 группа": {"at": "x", "dates": ["2026-10-28"]}}})
    assert routes.schedule_freshness(GroupRef("49.03.04", 1, "1 группа")) == \
        {"updated": "27.10", "changed": {"at": "x", "dates": ["2026-10-28"]}}  # 22:30 UTC - уже 27-е по Москве
    assert routes.schedule_freshness(GroupRef("49.03.04", 1, "2 группа")) == {"updated": "27.10"}
    monkeypatch.setattr(lms_sync, "status", {"checked_at": None, "changed": {}})
    assert routes.schedule_freshness(GroupRef("49.03.04", 1, "1 группа")) == {}


def test_reload_marks_changed_groups(env, monkeypatch):
    """Перечитывание расписания (после подгрузки) отмечает группы, у которых поменялись известные дни."""
    import bot
    schedule_dir, conn = env
    monkeypatch.setattr(bot, "conn", conn)
    monkeypatch.setattr(bot, "SCHEDULE_DIR", schedule_dir)
    monkeypatch.setattr(bot, "day_marks", None)
    monkeypatch.setattr(lms_sync, "status", {"checked_at": None, "changed": {}})
    os.makedirs(os.path.join(schedule_dir, "1k"))
    shutil.copy(FILE_A, os.path.join(schedule_dir, "1k"))
    records = bot.load_schedules()
    first = min(str(r["date"])[:10] for r in records)
    monkeypatch.setattr(bot, "now_local", lambda: __import__("datetime").datetime.strptime(first, "%Y-%m-%d"))
    monkeypatch.setattr(bot, "day_marks", lms_sync.day_fingerprints(records, first))
    assert lms_sync.status["changed"] == {}
    target = next(r for r in records if str(r["date"])[:10] == first)
    changed = [dict(r, subject="Перенос") if r is target else r for r in records]
    monkeypatch.setattr(lms_sync, "merged_records", lambda c, d: changed)
    bot.load_schedules()
    assert list(lms_sync.status["changed"]) == [lms_sync.group_key(target)]
    assert lms_sync.status["changed"][lms_sync.group_key(target)]["dates"] == [first]


def _magistracy_page(schedule_dir):
    with open(os.path.join(schedule_dir, "lms_sources.json"), "w", encoding="utf-8") as f:
        json.dump({"base_url": "https://lms.test", "pages": [
            {"url": "/course/view.php?id=5", "include": "расписание", "level": "m"}]}, f)


def test_magistracy_page_takes_only_magistracy_programs(env, monkeypatch):
    """Со страницы магистратуры файл бакалавриата не принимается, а из смешанного берутся только направления XX.04.XX."""
    schedule_dir, conn = env
    _magistracy_page(schedule_dir)
    report = run(env, FakeMoodle())  # в файле 49.03.02: бакалавриат
    assert "направлений магистратуры в файле не нашлось" in report.text() and list_synced_files(conn) == []

    def lesson(program, group):
        return {"program": program, "program_name": "x", "course": 1, "group": group, "week_sheet": "01.09",
                "day": "Понедельник", "date": "2026-09-01", "time": "8:50-10:20", "subject": "Лекция"}

    monkeypatch.setattr(lms_sync, "parse_bytes", lambda content, ext, d: [
        lesson("38.04.02", "1 группа"), lesson("49.03.04", "1 группа"), lesson("", "2 группа")])
    report = run(env, FakeMoodle())
    assert report.changed and "пропущены группы не магистратуры" in report.text()
    assert [json.loads(i["group_counts"]) for i in list_synced_files(conn)] == [{"38.04.02|1|1 группа": 1}]
    assert [lms_sync.group_key(r) for r in merged_records(conn, schedule_dir)] == ["38.04.02|1|1 группа"]


def test_renamed_groups_in_previous_version_are_not_counted_as_lost(env, monkeypatch):
    """Прошлая версия файла сохранена, когда парсер называл группы иначе (магистратура: «Направленность: ...»
    стала «МП-01-26»). Новая версия сравнивается с прошлой в нынешнем прочтении и принимается."""
    schedule_dir, conn = env
    original = open(FILE_A, "rb").read()
    assert run(env, FakeMoodle()).changed
    key = list_synced_files(conn)[0]["key"]
    conn.execute("UPDATE synced_files SET group_counts = ? WHERE key = ?",
                 (json.dumps({"49.03.02|1|Направленность: старое имя": 300}), key))
    conn.commit()
    real_parse = lms_sync.parse_bytes
    monkeypatch.setattr(lms_sync, "parse_bytes", lambda content, ext, d: real_parse(original, ext, d))
    report = run(env, FakeMoodle({"100": ("t2-09-26.xls", original + b"\0")}))  # новая версия: другой sha
    assert "обновлён" in report.text() and "пропали группы" not in report.text()
    assert "Направленность" not in list_synced_files(conn)[0]["group_counts"]
