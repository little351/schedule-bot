import os
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from telegram import Update

from database import (
    get_user_group, get_relative_week_sheet, get_week_schedule_grid,
    DAYS_ORDER, TIME_SLOTS, WEEKDAY_MAP, get_day_lessons,
    get_groups, set_user_group
)
from formatting import trim_trailing_empty
from bot import build_application, post_init, conn, load_schedules

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
    # база на бесплатном тарифе обнуляется при "засыпании" - при каждом холодном
    # старте перезаливаем расписание из файлов, которые лежат в самом репозитории (data/*.xlsx)
    existing = conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
    if existing == 0:
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


@app.get("/api/day")
def get_day(telegram_id: int, date: str):
    group = get_user_group(conn, telegram_id)
    if not group:
        raise HTTPException(status_code=404, detail="group_not_set")

    try:
        target_date = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="bad_date")

    week_sheet, lessons = get_day_lessons(conn, group, target_date)
    if not week_sheet:
        raise HTTPException(status_code=404, detail="week_not_found")

    day_name = WEEKDAY_MAP.get(target_date.weekday(), "Воскресенье")

    return {
        "group": group,
        "week_sheet": week_sheet,
        "day_name": day_name,
        "date": date,
        "lessons": lessons,
    }


@app.get("/api/groups")
def list_groups(telegram_id: int = 0):
    return {"groups": get_groups(conn), "current": get_user_group(conn, telegram_id)}


class GroupBody(BaseModel):
    telegram_id: int
    group: str


@app.post("/api/group")
def choose_group(body: GroupBody):
    if body.group not in get_groups(conn):
        raise HTTPException(status_code=400, detail="unknown_group")
    set_user_group(conn, body.telegram_id, body.group)
    return {"group": body.group}


def build_day_slots(grid, day_name):
    subjects = [grid[day_name][t] for t in TIME_SLOTS]
    trimmed = trim_trailing_empty(subjects)
    if trimmed is None:
        return None
    return [{"time": t.replace(".", ":"), "subject": s} for t, s in zip(TIME_SLOTS, trimmed)]


@app.get("/api/schedule")
def get_schedule(telegram_id: int, offset: int = 0):
    group = get_user_group(conn, telegram_id)
    if not group:
        raise HTTPException(status_code=404, detail="group_not_set")

    week_sheet = get_relative_week_sheet(conn, group, offset)
    if not week_sheet:
        raise HTTPException(status_code=404, detail="week_not_found")

    grid = get_week_schedule_grid(conn, group, week_sheet)

    days_out = []
    for day_name in DAYS_ORDER:
        slots = build_day_slots(grid, day_name)
        if slots:
            days_out.append({"day": day_name, "slots": slots})

    return {"group": group, "week_sheet": week_sheet, "days": days_out}


app.mount("/", StaticFiles(directory="webapp", html=True), name="webapp")