import os
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import init_db  # noqa: E402
from routes import build_router  # noqa: E402
from test_notes import TOKEN, init_data  # noqa: E402


def test_game_leaderboard_keeps_best_score_per_player(tmp_path):
    conn = init_db(str(tmp_path / "g.db"))
    app = FastAPI()
    app.include_router(build_router(conn, TOKEN))
    client = TestClient(app)

    def post(user_id, score):
        return client.post("/api/game/score", json={"score": score}, headers={"X-Init-Data": init_data(user_id)})

    assert client.post("/api/game/score", json={"score": 10}).status_code == 403
    assert post(1, -5).status_code == 400 and post(1, 10**9).status_code == 400
    assert post(1, 300).json() == {"top": [{"name": "Т", "score": 300, "me": True}], "best": 300, "rank": 1}
    post(2, 500)
    r = post(1, 100).json()  # худшая партия рекорд не портит
    assert r["best"] == 300 and r["rank"] == 2 and [row["score"] for row in r["top"]] == [500, 300]
    for uid in range(3, 15):
        post(uid, uid)
    assert len(client.get("/api/game/top", headers={"X-Init-Data": init_data(1)}).json()["top"]) == 10
    anon = client.get("/api/game/top").json()  # без подписи таблица видна, личного рекорда нет
    assert anon["best"] is None and anon["top"][0]["score"] == 500
