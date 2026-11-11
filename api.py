"""Локальный сервер для разработки мини-приложения без бота и вебхука:  uvicorn api:app"""
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from database import init_db, save_schedule
from parser import parse_all_schedules
from routes import add_common_middleware, build_router

conn = init_db()
save_schedule(conn, parse_all_schedules("data"))

app = FastAPI()
add_common_middleware(app)
app.include_router(build_router(conn, bot_token=""))
app.mount("/", StaticFiles(directory="webapp", html=True), name="webapp")
