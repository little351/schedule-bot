"""Автозадачи: копия раньше подгрузки, оборванная подгрузка, ошибки владельцу, /health, вызов /cron из GitHub."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "123:abc")

from database import get_sync_state, init_db, set_sync_state  # noqa: E402


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text):
        self.sent.append(("text", text))

    async def send_document(self, chat_id, document, filename, caption):
        self.sent.append(("doc", caption))


def setup(tmp_path, monkeypatch):
    import bot
    conn = init_db(str(tmp_path / "j.db"))
    monkeypatch.setattr(bot, "conn", conn)
    monkeypatch.setattr(bot, "OWNER_ID", "42")
    return bot, conn


def ago(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat(timespec="seconds")


def test_backup_goes_first_and_weekly(tmp_path, monkeypatch):
    bot, conn = setup(tmp_path, monkeypatch)
    order = []
    monkeypatch.setattr(bot, "sync_ready", lambda: True)
    monkeypatch.setattr(bot.lms_sync, "is_due", lambda *a, **k: True)

    async def fake_sync():
        order.append("sync")
        set_sync_state(conn, "last_run", ago(seconds=0))
        return SimpleNamespace(problems=[])
    monkeypatch.setattr(bot, "run_lms_sync", fake_sync)
    fake = FakeBot()
    orig = fake.send_document

    async def doc(**kw):
        order.append("backup")
        await orig(**kw)
    fake.send_document = doc
    assert asyncio.run(bot.run_due_jobs(fake, "тест"))
    assert order == ["backup", "sync"]
    asyncio.run(bot.run_due_jobs(fake, "тест"))
    assert order.count("backup") == 1  # неделя не прошла


def test_interrupted_sync_is_reported_and_not_retried_at_once(tmp_path, monkeypatch):
    bot, conn = setup(tmp_path, monkeypatch)
    set_sync_state(conn, "backup_sent_at", ago(days=1))
    set_sync_state(conn, "last_run", ago(days=2))
    set_sync_state(conn, "lms_started_at", ago(hours=1))
    monkeypatch.setattr(bot, "sync_ready", lambda: True)
    monkeypatch.setattr(bot.lms_sync, "is_due", lambda *a, **k: True)
    runs = []

    async def fake_sync():
        runs.append(1)
        return SimpleNamespace(problems=[])
    monkeypatch.setattr(bot, "run_lms_sync", fake_sync)
    fake = FakeBot()
    asyncio.run(bot.run_due_jobs(fake, "тест"))
    assert runs == []  # оборвалась час назад: ждём, чтобы не попасть в круг
    set_sync_state(conn, "lms_started_at", ago(hours=7))
    asyncio.run(bot.run_due_jobs(fake, "тест"))
    assert runs == [1] and any("не дошла до конца" in t for _, t in fake.sent)
    assert get_sync_state(conn, "lms_started_at") > ago(minutes=1)


def test_job_error_reaches_owner_once_and_other_jobs_still_run(tmp_path, monkeypatch):
    bot, conn = setup(tmp_path, monkeypatch)

    def broken(conn):
        raise RuntimeError("база недоступна")
    monkeypatch.setattr(bot, "make_backup", broken)
    monkeypatch.setattr(bot, "sync_ready", lambda: True)
    monkeypatch.setattr(bot.lms_sync, "is_due", lambda *a, **k: True)
    runs = []

    async def fake_sync():
        runs.append(1)
        set_sync_state(conn, "last_run", ago(seconds=0))  # успешная подгрузка пишет время окончания
        return SimpleNamespace(problems=[])
    monkeypatch.setattr(bot, "run_lms_sync", fake_sync)
    fake = FakeBot()
    asyncio.run(bot.run_due_jobs(fake, "тест"))
    asyncio.run(bot.run_due_jobs(fake, "тест"))
    errors = [t for _, t in fake.sent if "упала" in t]
    assert len(errors) == 1 and "база недоступна" in errors[0] and len(runs) == 2
    assert "резервная копия" in bot.format_health()


def test_stale_sync_warning_and_health(tmp_path, monkeypatch):
    bot, conn = setup(tmp_path, monkeypatch)
    set_sync_state(conn, "backup_sent_at", ago(days=1))
    set_sync_state(conn, "last_run", ago(hours=40))
    set_sync_state(conn, "last_ok", "1")
    monkeypatch.setattr(bot, "sync_ready", lambda: True)
    monkeypatch.setattr(bot, "lms_if_due", lambda b: asyncio.sleep(0))
    fake = FakeBot()
    asyncio.run(bot.run_due_jobs(fake, "тест"))
    assert any("Подгрузки расписания не было" in t for _, t in fake.sent)
    text = bot.format_health()
    assert "Сервер запущен" in text and "Последняя:" in text and "Резервная копия" in text


def test_cron_endpoint_needs_secret(monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://example.org")
    import importlib
    import bot
    monkeypatch.setattr(bot, "TOKEN", "123:abc")
    import main
    main = importlib.reload(main)
    calls = []

    async def fake_jobs(b, source):
        calls.append(source)
    monkeypatch.setattr(main, "run_due_jobs", fake_jobs)
    client = TestClient(main.app)
    monkeypatch.setattr(main, "CRON_SECRET", "")
    assert client.post("/cron", headers={"X-Cron-Secret": ""}).status_code == 404  # не настроено: адреса нет
    monkeypatch.setattr(main, "CRON_SECRET", "s3cret")
    assert client.post("/cron", headers={"X-Cron-Secret": "wrong"}).status_code == 404
    assert client.post("/cron", headers={"X-Cron-Secret": "s3cret"}).json() == {"ok": True}
    assert calls == ["вызов из GitHub"]
