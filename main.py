import asyncio
import hashlib
import hmac
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from telegram import Update

from bot import (build_application, post_init, conn, load_schedules, auto_scheduler, run_due_jobs, send_campus_map, TOKEN,
                 stats_flusher, stats_flush_now)
from routes import build_router

PUBLIC_URL = os.environ["PUBLIC_URL"].rstrip("/")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "hook")
WEBHOOK_PATH = f"/telegram/{WEBHOOK_SECRET}"
# Telegram присылает этот токен в заголовке каждого обновления: без него запрос не от Telegram.
# Выводится из токена бота, поэтому одинаков при каждом запуске и не угадывается снаружи
TELEGRAM_SECRET_TOKEN = hashlib.sha256(f"webhook:{TOKEN}".encode()).hexdigest()

application = build_application()


@asynccontextmanager
async def lifespan(app):
    # при каждом старте перезаливаем расписание из data/ (.xlsx и .xls, включая подпапки data/1k ... data/5k): файлы - источник истины,
    # так новый файл подхватывается простым перезапуском (таблица users не трогается)
    load_schedules()

    await application.initialize()
    await post_init(application)
    await application.bot.set_webhook(url=f"{PUBLIC_URL}{WEBHOOK_PATH}", secret_token=TELEGRAM_SECRET_TOKEN)
    # автозадачи, пока сервер не спит: еженедельная копия владельцу (нужен OWNER_ID) и подгрузка расписания
    # с сайта вуза (нужны LMS_LOGIN и LMS_PASSWORD); раз в сутки их же будит /cron из GitHub Actions
    # статистика действий копится в памяти и раз в 10 минут уходит в базу
    tasks = [asyncio.create_task(auto_scheduler(application)), asyncio.create_task(stats_flusher())]
    yield
    for task in tasks:
        task.cancel()
    await stats_flush_now()  # Render останавливает сервер (сон, деплой): несохранённые счётчики не теряются
    await application.shutdown()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    received = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(received, TELEGRAM_SECRET_TOKEN):
        raise HTTPException(status_code=403)
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return {"ok": True}


# Раз в сутки GitHub Actions (.github/workflows/daily-jobs.yml) будит сервер и запускает автозадачи:
# бесплатный Render спит без запросов, и таймер внутри процесса тогда не идёт. Секрет передаётся заголовком
# (не в адресе, чтобы не попадал в логи); без CRON_SECRET адрес не работает вовсе.
CRON_SECRET = os.environ.get("CRON_SECRET", "").strip()
_cron_tasks = set()


@app.post("/cron")
async def cron(request: Request):
    received = request.headers.get("X-Cron-Secret", "")
    if not CRON_SECRET or not hmac.compare_digest(received, CRON_SECRET):
        raise HTTPException(status_code=404)
    # отвечаем сразу, задачи идут в фоне: после запроса Render ещё 15 минут не усыпляет сервер
    task = asyncio.create_task(run_due_jobs(application.bot, "вызов из GitHub"))
    _cron_tasks.add(task)
    task.add_done_callback(_cron_tasks.discard)
    return {"ok": True}


app.include_router(build_router(conn, TOKEN, send_campus_map=lambda chat_id: send_campus_map(application.bot, chat_id)))


app.mount("/", StaticFiles(directory="webapp", html=True), name="webapp")