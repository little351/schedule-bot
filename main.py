import os
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from telegram import Update

from parser import parse_schedule
from database import (
    get_user_group, get_relative_week_sheet, get_week_schedule_grid,
    DAYS_ORDER, TIME_SLOTS, WEEKDAY_MAP, get_day_lessons,
    set_rating, get_rating_summary, get_user_rating, RATING_OPTIONS,
    save_schedule
)
from formatting import trim_trailing_empty
from bot import build_application, post_init, conn, SCHEDULE_PATH

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
    # старте перезаливаем расписание из файла, который лежит в самом репозитории
    existing = conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
    if existing == 0 and os.path.exists(SCHEDULE_PATH):
        records = parse_schedule(SCHEDULE_PATH)
        save_schedule(conn, records)

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

    for lesson in lessons:
        key = lesson["subject"]
        summary = get_rating_summary(conn, key)
        lesson["subject_key"] = key
        lesson["rating"] = summary
        lesson["my_vote"] = get_user_rating(conn, key, telegram_id)

    day_name = WEEKDAY_MAP.get(target_date.weekday(), "Воскресенье")

    return {
        "group": group,
        "week_sheet": week_sheet,
        "day_name": day_name,
        "date": date,
        "lessons": lessons,
    }


class VoteBody(BaseModel):
    telegram_id: int
    subject_key: str
    option: str


@app.post("/api/rate")
def rate(body: VoteBody):
    if body.option not in RATING_OPTIONS:
        raise HTTPException(status_code=400, detail="bad_option")
    set_rating(conn, body.subject_key, body.telegram_id, body.option)
    return get_rating_summary(conn, body.subject_key)


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

    week_sheet = get_relative_week_sheet(conn, offset)
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