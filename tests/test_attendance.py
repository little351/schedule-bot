"""Отметка посещаемости через бота (/mark): разбор страниц Moodle «Посещаемость», отправка формы, команда владельца."""
import asyncio
import json
import os
import sys
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("BOT_TOKEN", "123:abc")

import attendance  # noqa: E402
import lms_sync  # noqa: E402
from database import init_db  # noqa: E402
from lms_sync import MoodleClient  # noqa: E402

LOGIN, PASSWORD = "student42", "S3cret-pass"
LOGIN_PAGE = '<form id="login"><input type="hidden" name="logintoken" value="tok123"><input name="username"></form>'


def view_page(open_session):
    link = ('<a href="https://lms.test/mod/attendance/attendance.php?sessid=777&amp;sesskey=abc">Отметить свое '
            'присутствие</a>') if open_session else "?"
    return f"""<html><head><title>ТиМ футбола: Посещаемость</title></head><body>
<a href="/login/logout.php?sesskey=abc">Выход</a>
<table><tr><td>10.11.26</td><td>10:40 - 12:10</td><td>{link}</td></tr></table></body></html>"""


def form_page(password):
    pwd = '<input type="password" name="studentpassword" id="id_studentpassword">' if password else ""
    return f"""<html><body><a href="/login/logout.php?sesskey=abc">Выход</a>
<form autocomplete="off" action="https://lms.test/mod/attendance/attendance.php" method="post" class="mform">
<input name="sessid" type="hidden" value="777" />
<input name="sesskey" type="hidden" value="abc" />
<input name="_qf__mod_attendance_form_studentattendance" type="hidden" value="1" />
{pwd}
<label class="form-check-inline form-check-label fitem"><input type="radio" class="form-check-input" name="status"
 id="id_status_11" value="11"> Присутствовал</label>
<label class="form-check-inline form-check-label fitem"><input type="radio" class="form-check-input" name="status"
 id="id_status_12" value="12"> Опоздал</label>
<input type="submit" class="btn btn-primary" name="submitbutton" id="id_submitbutton" value="Сохранить">
<input type="submit" class="btn btn-secondary" name="cancel" id="id_cancel" value="Отмена">
</form></body></html>"""


class FakeAttendance:
    """Подобие Moodle с одной страницей посещаемости и открытой самоотметкой sessid=777."""

    def __init__(self, password=None, accept=True):
        self.password, self.accept = password, accept
        self.logged_in = False
        self.open = True
        self.posts = []

    def __call__(self, request):
        path = request.url.path
        if path == "/login/index.php" and request.method == "GET":
            return httpx.Response(200, text=LOGIN_PAGE)
        if path == "/login/index.php":
            self.logged_in = True
            return httpx.Response(200, text='<a href="/login/logout.php?sesskey=abc">Выход</a>')
        if not self.logged_in:
            return httpx.Response(200, text=LOGIN_PAGE)
        if path == "/mod/attendance/view.php":
            if request.url.params["id"] == "404":
                return httpx.Response(404, text="нет")
            return httpx.Response(200, text=view_page(self.open))
        if path == "/mod/attendance/attendance.php" and request.method == "GET":
            return httpx.Response(200, text=form_page(self.password))
        if path == "/mod/attendance/attendance.php":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.posts.append(form)
            if self.password and form.get("studentpassword") != self.password:
                return httpx.Response(200, text=form_page(True) + '<div class="alert alert-danger">Неверный пароль</div>')
            if self.accept:
                self.open = False
            return httpx.Response(303, headers={"location": "/mod/attendance/view.php?id=505662"})
        return httpx.Response(404, text="нет")


def client(site):
    c = MoodleClient("https://lms.test", LOGIN, PASSWORD, transport=httpx.MockTransport(site), pause=0)
    c.login()
    return c


def test_page_path_accepts_links_and_numbers():
    assert attendance.page_path("https://lms.sportedu.ru/mod/attendance/view.php?id=505662") == \
        "/mod/attendance/view.php?id=505662"
    assert attendance.page_path("505662") == "/mod/attendance/view.php?id=505662"
    assert attendance.page_path("https://lms.sportedu.ru/course/view.php?id=5") is None


def test_form_is_parsed_with_wrapped_labels_and_without_cancel():
    form = attendance.parse_form(form_page(True), "https://lms.test/mod/attendance/attendance.php?sessid=777")
    assert form["statuses"] == [("11", "Присутствовал"), ("12", "Опоздал")]
    assert form["password"] == "studentpassword"
    assert form["fields"]["sessid"] == "777" and "submitbutton" in form["fields"] and "cancel" not in form["fields"]
    assert attendance.present_status(form["statuses"]) == ("11", "Присутствовал")


def test_find_open_and_mark_present():
    site = FakeAttendance()
    c = client(site)
    found, problems = attendance.find_open(c, ["/mod/attendance/view.php?id=505662", "/mod/attendance/view.php?id=404"])
    assert found == [{"page": "/mod/attendance/view.php?id=505662", "title": "ТиМ футбола: Посещаемость", "sessid": "777"}]
    assert len(problems) == 1 and "404" in problems[0]
    assert attendance.mark(c, found[0]["page"], "777") == ("ok", "Присутствовал")
    assert site.posts[0]["status"] == "11" and site.posts[0]["sesskey"] == "abc" and "cancel" not in site.posts[0]


def test_password_is_asked_and_wrong_one_is_reported():
    site = FakeAttendance(password="4321")
    c = client(site)
    page = "/mod/attendance/view.php?id=505662"
    assert attendance.mark(c, page, "777") == ("password", "")
    assert site.posts == []  # без пароля ничего не отправили
    assert attendance.mark(c, page, "777", "0000") == ("error", "Неверный пароль")
    assert attendance.mark(c, page, "777", "4321") == ("ok", "Присутствовал")


def test_not_accepted_and_closed_are_errors_not_success():
    site = FakeAttendance(accept=False)
    c = client(site)
    page = "/mod/attendance/view.php?id=505662"
    result, reason = attendance.mark(c, page, "777")
    assert result == "error" and "не принял" in reason
    site.open = False
    assert attendance.mark(c, page, "777")[0] == "error"


def test_no_present_status_sends_nothing():
    html = form_page(False).replace("Присутствовал", "Отсутствовал")
    form = attendance.parse_form(html, "https://lms.test/x")
    assert attendance.present_status(form["statuses"]) is None


def test_describe_lesson_picks_current_or_next():
    import bot
    lessons = [{"time_start": "10:40", "time_end": "12:10", "subject": "Футбол"},
               {"time_start": "13:00", "time_end": "14:30", "subject": "Анатомия"}]
    assert bot.describe_lesson(lessons, datetime(2026, 11, 10, 10, 30)) == "Сейчас 2 пара, 10:40–12:10: «Футбол»."
    assert "следующая: 3 пара, 13:00–14:30: «Анатомия»" in bot.describe_lesson(lessons, datetime(2026, 11, 10, 12, 20))
    assert bot.describe_lesson(lessons, datetime(2026, 11, 10, 15, 0)) == "Сейчас по расписанию пары нет."


@pytest.fixture
def owner(tmp_path, monkeypatch):
    import bot
    monkeypatch.setenv("LMS_LOGIN", LOGIN)
    monkeypatch.setenv("LMS_PASSWORD", PASSWORD)
    schedule_dir = tmp_path / "data"
    schedule_dir.mkdir()
    (schedule_dir / "lms_sources.json").write_text(json.dumps({"base_url": "https://lms.test", "pages": []}))
    monkeypatch.setattr(bot, "conn", init_db(str(tmp_path / "a.db")))
    monkeypatch.setattr(bot, "SCHEDULE_DIR", str(schedule_dir))
    monkeypatch.setattr(bot, "OWNER_ID", "42")
    site = FakeAttendance()
    monkeypatch.setattr(lms_sync, "MoodleClient",
                        lambda b, l, p: MoodleClient(b, l, p, transport=httpx.MockTransport(site), pause=0))
    sent, tasks = [], []

    async def reply_text(text, **kw):
        sent.append((text, kw.get("reply_markup")))

    async def send_message(chat_id, text):
        sent.append((text, None))

    message = SimpleNamespace(reply_text=reply_text, chat=SimpleNamespace(id=42), text="")
    update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=42))
    context = SimpleNamespace(args=[], bot_data={}, user_data={}, bot=SimpleNamespace(send_message=send_message),
                              application=SimpleNamespace(create_task=tasks.append))
    return SimpleNamespace(bot=bot, site=site, sent=sent, tasks=tasks, update=update, context=context)


def run(o, coro):
    async def go():
        await coro
        while o.tasks:
            await o.tasks.pop(0)
    asyncio.run(go())


def test_mark_pages_add_list_del(owner):
    o = owner
    o.context.args = ["add", "https://lms.sportedu.ru/mod/attendance/view.php?id=505662", "123"]
    run(o, o.bot.mark_command(o.update, o.context))
    assert o.bot.mark_pages() == ["/mod/attendance/view.php?id=505662", "/mod/attendance/view.php?id=123"]
    o.context.args = ["del", "2"]
    run(o, o.bot.mark_command(o.update, o.context))
    assert o.bot.mark_pages() == ["/mod/attendance/view.php?id=505662"]
    assert "id=505662" in o.sent[-1][0]


def test_mark_asks_then_marks_only_after_yes(owner):
    o = owner
    o.context.args = ["add", "505662"]
    run(o, o.bot.mark_command(o.update, o.context))
    o.context.args = []
    run(o, o.bot.mark_command(o.update, o.context))
    question, markup = o.sent[-1]
    assert "ТиМ футбола" in question and markup is not None
    assert o.site.posts == []  # до «Да» ничего не отправлено
    yes = markup.inline_keyboard[0][0].callback_data
    edits = []

    async def answer():
        pass

    async def edit_message_text(text, **kw):
        edits.append(text)

    o.update.callback_query = SimpleNamespace(data=yes, answer=answer, edit_message_text=edit_message_text,
                                              message=SimpleNamespace(chat=SimpleNamespace(id=42)))
    run(o, o.bot.mark_button(o.update, o.context))
    assert o.site.posts and o.sent[-1][0].startswith("✅ Отмечено")
    run(o, o.bot.mark_button(o.update, o.context))  # повторное нажатие: вопрос уже использован
    assert "устарел" in edits[-1] and len(o.site.posts) == 1
    assert PASSWORD not in "".join(t for t, _ in o.sent)


def test_mark_password_comes_from_next_message(owner):
    o = owner
    o.site.password = "4321"
    o.context.args = ["add", "505662"]
    run(o, o.bot.mark_command(o.update, o.context))
    o.context.args = []
    run(o, o.bot.mark_command(o.update, o.context))
    yes = o.sent[-1][1].inline_keyboard[0][0].callback_data

    async def noop(*a, **k):
        pass

    o.update.callback_query = SimpleNamespace(data=yes, answer=noop, edit_message_text=noop,
                                              message=SimpleNamespace(chat=SimpleNamespace(id=42)))
    run(o, o.bot.mark_button(o.update, o.context))
    assert "нужен пароль" in o.sent[-1][0]
    o.update.message.text = " 4321 "
    run(o, o.bot.handle_message(o.update, o.context))
    assert o.sent[-1][0].startswith("✅ Отмечено") and o.site.posts[-1]["studentpassword"] == "4321"


def test_no_open_mark_is_reported(owner):
    o = owner
    o.site.open = False
    o.context.args = ["add", "505662"]
    run(o, o.bot.mark_command(o.update, o.context))
    o.context.args = []
    run(o, o.bot.mark_command(o.update, o.context))
    assert "Открытой отметки сейчас нет" in o.sent[-1][0]
