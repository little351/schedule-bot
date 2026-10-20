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
