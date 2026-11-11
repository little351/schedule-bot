"""Общие настройки сервера: сжатие ответов и индекс расписания."""
import gzip
import os
import sys
from datetime import datetime

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import GroupRef, get_day_lessons, init_db, save_schedule  # noqa: E402
from routes import add_common_middleware, build_router  # noqa: E402
from test_notes import TOKEN  # noqa: E402


def make_app(conn):
    app = FastAPI()
    add_common_middleware(app)
    app.include_router(build_router(conn, TOKEN))
    app.mount("/", StaticFiles(directory=os.path.join(ROOT, "webapp"), html=True), name="webapp")
    return app


def test_miniapp_is_sent_compressed_and_unchanged(tmp_path):
    client = TestClient(make_app(init_db(str(tmp_path / "s.db"))))
    original = open(os.path.join(ROOT, "webapp", "index.html"), "rb").read()
    r = client.get("/", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200 and r.headers["content-encoding"] == "gzip"
    assert r.num_bytes_downloaded < len(original) / 2  # по сети ушло сжатое
    assert r.content == original  # клиент сам распаковывает: страница та же самая
    js = client.get("/tetris.js?v=11", headers={"Accept-Encoding": "gzip"})
    assert js.headers["content-encoding"] == "gzip"


def test_no_compression_without_accept_encoding(tmp_path):
    client = TestClient(make_app(init_db(str(tmp_path / "s.db"))))
    r = client.get("/", headers={"Accept-Encoding": "identity"})
    assert r.status_code == 200 and "content-encoding" not in r.headers


def test_cache_revalidation_still_works(tmp_path):
    """Telegram держит мини-апп в кеше и переспрашивает по ETag: со сжатием ответ по-прежнему 304."""
    client = TestClient(make_app(init_db(str(tmp_path / "s.db"))))
    etag = client.get("/", headers={"Accept-Encoding": "gzip"}).headers["etag"]
    r = client.get("/", headers={"Accept-Encoding": "gzip", "If-None-Match": etag})
    assert r.status_code == 304


def test_small_api_answers_and_cors_unchanged(tmp_path):
    client = TestClient(make_app(init_db(str(tmp_path / "s.db"))))
    r = client.get("/api/catalog", headers={"Accept-Encoding": "gzip", "Origin": "https://web.telegram.org"})
    assert r.status_code == 200 and "content-encoding" not in r.headers  # меньше 1 КБ не сжимается
    assert r.headers["access-control-allow-origin"] == "*"


def test_lessons_index_is_used_and_results_unchanged(tmp_path):
    conn = init_db(str(tmp_path / "s.db"))
    records = [
        {"week_sheet": "11.11", "day": "Среда", "date": datetime(2026, 11, 11), "time": "8.50-10.20",
         "group": g, "subject": f"Предмет {g}", "program": "49.03.04", "program_name": "ФК", "course": 1}
        for g in ("1 группа", "2 группа")
    ]
    save_schedule(conn, records)
    plan = " ".join(str(row) for row in conn.execute(
        "EXPLAIN QUERY PLAN SELECT day, time, subject FROM lessons "
        "WHERE program=? AND course=? AND group_name=? AND week_sheet=?", ("49.03.04", 1, "1 группа", "11.11")))
    assert "lessons_group" in plan
    sheet, lessons = get_day_lessons(conn, GroupRef("49.03.04", 1, "1 группа"), datetime(2026, 11, 11))
    assert sheet == "11.11" and [l["subject"] for l in lessons] == ["Предмет 1 группа"]
    init_db(str(tmp_path / "s.db"))  # повторный старт на той же базе не падает
