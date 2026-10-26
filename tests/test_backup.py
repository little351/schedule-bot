"""Полная резервная копия: пользователи, заметки, рекорды игры и состояние подгрузки."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import (  # noqa: E402
    GroupRef, add_note, export_backup, game_leaderboard, import_backup, init_db, list_notes, list_users,
    save_game_score, set_sync_state, set_user_group, get_sync_state,
)


def fill(conn):
    set_user_group(conn, 1, GroupRef("49.03.04", 2, "4 группа"))
    set_user_group(conn, 2, GroupRef("49.03.01", 1, "Т1-04-26"))
    add_note(conn, 1, "Философия", "реферат", "2026-11-01")
    add_note(conn, 2, "", "без предмета")
    save_game_score(conn, 1, "Иван", 120)
    set_sync_state(conn, "active_keys", '["a.xls"]')


def test_backup_restores_everything_into_empty_db(tmp_path):
    os.environ.setdefault("BOT_TOKEN", "123:abc")
    import bot
    source = init_db(str(tmp_path / "a.db"))
    fill(source)
    name, raw, caption = bot.make_backup(source)
    assert name.startswith("backup-") and name.endswith(".json")
    assert "пользователей 2" in caption and "заметок 2" in caption and "рекордов игры 1" in caption

    target = init_db(str(tmp_path / "b.db"))
    data = bot.parse_backup(raw)
    assert import_backup(target, data) == {"users": 2, "notes": 2, "game_scores": 1, "sync_state": 1}
    assert export_backup(target) == export_backup(source)
    assert [n["text"] for n in list_notes(target, 1)] == ["реферат"]
    assert game_leaderboard(target, 1)["best"] == 120
    # повторное восстановление ничего не дублирует, а более свежие данные в базе не перетираются
    set_user_group(target, 1, GroupRef("49.03.04", 3, "5 группа"))
    assert import_backup(target, data) == {"users": 0, "notes": 0, "game_scores": 0, "sync_state": 0}
    assert len(list_notes(target, 1)) == 1
    assert [u for u in list_users(target) if u[0] == 1][0][3] == "5 группа"
    # после восстановления новые заметки добавляются как обычно
    add_note(target, 1, "История", "доклад")
    assert len(list_notes(target, 1)) == 2


def test_backup_rejects_foreign_files_and_unknown_columns(tmp_path):
    os.environ.setdefault("BOT_TOKEN", "123:abc")
    import bot
    assert bot.parse_backup(b"not json") is None
    assert bot.parse_backup(json.dumps({"tables": {}}).encode()) is None
    conn = init_db(str(tmp_path / "c.db"))
    data = {"kind": bot.BACKUP_KIND, "tables": {
        "users": [{"telegram_id": 5, "group_name": "1 группа", "evil; DROP TABLE users": 1}],
        "lessons": [{"week_sheet": "x"}],
        "notes": [{"text": "без владельца"}, "мусор"],
    }}
    assert import_backup(conn, data)["users"] == 1
    assert [u[0] for u in list_users(conn)] == [5]
    assert list_notes(conn, None) == []
    assert get_sync_state(conn, "active_keys") is None
