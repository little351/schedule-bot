import hashlib
import hmac
import json
import os
import sys
import time
from urllib.parse import urlencode

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import init_db  # noqa: E402
from routes import build_router  # noqa: E402

TOKEN = "123:test-token"


def init_data(user_id):
    """Подписанные данные мини-аппа, как их выдаёт Telegram."""
    params = {"auth_date": str(int(time.time())), "user": json.dumps({"id": user_id, "first_name": "Т"})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    params["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(params)


def make_client(tmp_path):
    conn = init_db(str(tmp_path / "notes.db"))
    app = FastAPI()
    app.include_router(build_router(conn, TOKEN))
    return TestClient(app)


def test_notes_are_private_and_need_signed_data(tmp_path):
    client = make_client(tmp_path)
    assert client.get("/api/notes").status_code == 403
    assert client.get("/api/notes", headers={"X-Init-Data": "hash=bad"}).status_code == 403

    mine, other = {"X-Init-Data": init_data(1)}, {"X-Init-Data": init_data(2)}
    r = client.post("/api/notes", json={"subject": "Физика", "text": "сдать лабу", "due_date": "2026-11-01"}, headers=mine)
    assert r.status_code == 200 and r.json()["notes"][0]["subject"] == "Физика"
    assert client.get("/api/notes", headers=other).json()["notes"] == []

    note_id = r.json()["notes"][0]["id"]
    # чужую заметку нельзя ни менять, ни удалять
    assert client.patch(f"/api/notes/{note_id}", json={"done": True}, headers=other).status_code == 404
    assert client.delete(f"/api/notes/{note_id}", headers=other).status_code == 404
    assert client.get("/api/notes", headers=mine).json()["notes"][0]["done"] is False


def test_note_lifecycle_and_order(tmp_path):
    client = make_client(tmp_path)
    h = {"X-Init-Data": init_data(7)}
    client.post("/api/notes", json={"subject": "А", "text": "без даты"}, headers=h)
    client.post("/api/notes", json={"subject": "Б", "text": "поздно", "due_date": "2026-12-01"}, headers=h)
    notes = client.post("/api/notes", json={"subject": "В", "text": "рано", "due_date": "2026-11-01"}, headers=h).json()["notes"]
    assert [n["subject"] for n in notes] == ["В", "Б", "А"]  # по дедлайну, без даты в конце

    early = notes[0]["id"]
    notes = client.patch(f"/api/notes/{early}", json={"done": True}, headers=h).json()["notes"]
    assert [n["subject"] for n in notes] == ["Б", "А", "В"] and notes[-1]["done"]  # сделанные уходят вниз
    notes = client.patch(f"/api/notes/{early}", json={"due_date": "", "text": "новый текст"}, headers=h).json()["notes"]
    assert notes[-1]["due_date"] is None and notes[-1]["text"] == "новый текст"
    notes = client.delete(f"/api/notes/{early}", headers=h).json()["notes"]
    assert len(notes) == 2


def test_note_validation(tmp_path):
    client = make_client(tmp_path)
    h = {"X-Init-Data": init_data(3)}
    assert client.post("/api/notes", json={"subject": " ", "text": ""}, headers=h).status_code == 400
    assert client.post("/api/notes", json={"text": "x", "due_date": "31.12"}, headers=h).status_code == 400
    note = client.post("/api/notes", json={"text": "я" * 900, "subject": "п" * 300}, headers=h).json()["notes"][0]
    assert len(note["text"]) == 500 and len(note["subject"]) == 100


def test_teachers_file_is_valid_and_served(tmp_path):
    from routes import load_teachers
    departments = load_teachers()
    assert departments and all(d["name"] and d["teachers"] for d in departments)
    assert all(t["name"] and t["id"] and t["position"] for d in departments for t in d["teachers"])
    assert len(departments) == 34 and len({t["id"] for d in departments for t in d["teachers"]}) == 376
    # один id - один человек: одинаковый id только у одинакового ФИО
    names = {}
    for d in departments:
        for t in d["teachers"]:
            assert names.setdefault(t["id"], t["name"]) == t["name"]
    assert load_teachers(str(tmp_path / "нет.json")) == []
    bad = tmp_path / "bad.json"
    bad.write_text("{не json", encoding="utf-8")
    assert load_teachers(str(bad)) == []
    client = make_client(tmp_path)
    assert client.get("/api/teachers").json()["departments"] == departments


def test_schedule_api_trusts_only_signed_user(tmp_path):
    """Чужой telegram_id в запросе ничего не даёт: пользователь берётся из подписи Telegram."""
    from datetime import datetime
    from database import GroupRef, get_user_group, save_schedule, set_user_group
    conn = init_db(str(tmp_path / "s.db"))
    rec = {"program": "49.03.04", "program_name": "Спорт", "course": 1, "week_sheet": "05.10",
           "day": "Понедельник", "date": datetime(2026, 10, 5), "time": "8.50-10.20", "subject": "Пара"}
    save_schedule(conn, [{**rec, "group": "1 группа"}, {**rec, "group": "2 группа"}])
    set_user_group(conn, 7, GroupRef("49.03.04", 1, "1 группа"))
    app = FastAPI()
    app.include_router(build_router(conn, TOKEN))
    client = TestClient(app)
    body = {"telegram_id": 7, "group": "2 группа", "program": "49.03.04", "course": 1}
    assert client.post("/api/group", json=body).status_code == 403
    assert client.get("/api/day?telegram_id=7&date=2026-10-05").status_code == 403
    assert client.get("/api/catalog?telegram_id=7").json()["current"] is None
    assert get_user_group(conn, 7).name == "1 группа"
    # свой выбор по подписи: telegram_id в теле игнорируется
    r = client.post("/api/group", json={**body, "telegram_id": 7}, headers={"X-Init-Data": init_data(8)})
    assert r.status_code == 200 and get_user_group(conn, 8).name == "2 группа" and get_user_group(conn, 7).name == "1 группа"
    h = {"X-Init-Data": init_data(8)}
    assert client.get("/api/catalog", headers=h).json()["current"]["group"] == "2 группа"
    assert client.get("/api/day?date=2026-10-05", headers=h).json()["lessons"][0]["subject"] == "Пара"


def test_webhook_rejects_requests_without_telegram_secret(monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://example.org")
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    import importlib
    import bot
    monkeypatch.setattr(bot, "TOKEN", "123:abc")
    import main
    main = importlib.reload(main)
    client = TestClient(main.app)
    assert client.post(main.WEBHOOK_PATH, json={"update_id": 1}).status_code == 403
    assert client.post(main.WEBHOOK_PATH, json={"update_id": 1},
                       headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"}).status_code == 403


def test_parallel_requests_and_reload_do_not_break_each_other(tmp_path):
    """API отвечает из нескольких потоков сразу, а расписание может перечитываться в это же время."""
    import threading
    from datetime import datetime
    from database import GroupRef, get_day_lessons, save_schedule
    conn = init_db(str(tmp_path / "p.db"))
    base = {"program": "49.03.04", "program_name": "Спорт", "course": 1, "group": "1 группа", "week_sheet": "05.10",
            "day": "Понедельник", "date": datetime(2026, 10, 5)}
    records = [{**base, "time": t, "subject": f"Пара {i}"}
               for i, t in enumerate(["8.50-10.20", "10.40-12.10", "13.00-14.30"])]
    records += [{**base, "group": f"{n} группа", "time": "8.50-10.20", "subject": "x"} for n in range(2, 400)]
    save_schedule(conn, records)
    group, problems, stop = GroupRef("49.03.04", 1, "1 группа"), [], threading.Event()

    def reader():
        while not stop.is_set():
            try:
                if len(get_day_lessons(conn, group, datetime(2026, 10, 5))[1]) != 3:
                    problems.append("неполный ответ")
            except Exception as e:  # noqa: BLE001
                problems.append(type(e).__name__)

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    for _ in range(20):
        save_schedule(conn, records)
    stop.set()
    for t in threads:
        t.join()
    assert problems == []
