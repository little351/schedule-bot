import os

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from telegram import Update

from bot import build_application, post_init, conn, load_schedules, TOKEN
from routes import build_router

PUBLIC_URL = os.environ["PUBLIC_URL"].rstrip("/")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "hook")
WEBHOOK_PATH = f"/telegram/{WEBHOOK_SECRET}"

app = FastAPI()
application = build_application()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    # при каждом старте перезаливаем расписание из data/ (.xlsx и .xls, включая подпапки data/1k ... data/5k): файлы - источник истины,
    # так новый файл подхватывается простым перезапуском (таблица users не трогается)
    load_schedules()

    await application.initialize()
    await post_init(application)
    await application.bot.set_webhook(url=f"{PUBLIC_URL}{WEBHOOK_PATH}")


@app.on_event("shutdown")
async def shutdown():
    await application.shutdown()


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return {"ok": True}


app.include_router(build_router(conn, TOKEN))


app.mount("/", StaticFiles(directory="webapp", html=True), name="webapp")