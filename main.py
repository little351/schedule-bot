import asyncio
import hashlib
import hmac
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from telegram import Update

from bot import build_application, post_init, conn, load_schedules, lms_scheduler, backup_scheduler, send_campus_map, TOKEN
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
    # автоподгрузка расписания с сайта вуза: работает, только если заданы LMS_LOGIN и LMS_PASSWORD
    tasks = [asyncio.create_task(lms_scheduler(application)),
             # еженедельная резервная копия владельцу в Telegram (нужен OWNER_ID)
             asyncio.create_task(backup_scheduler(application))]
    yield
    for task in tasks:
        task.cancel()
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


app.include_router(build_router(conn, TOKEN, send_campus_map=lambda chat_id: send_campus_map(application.bot, chat_id)))


app.mount("/", StaticFiles(directory="webapp", html=True), name="webapp")