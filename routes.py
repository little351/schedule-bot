"""HTTP API мини-приложения. Используется и боевым сервером (main.py), и локальным (api.py)."""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from database import (
    DAYS_ORDER, PRACTICE_SLOT, TIME_SLOTS, WEEKDAY_MAP, GroupRef, find_groups, get_catalog, get_day_lessons,
    get_groups, get_relative_week_sheet, get_user_group, get_week_schedule_grid, set_user_group,
    touch_user,
)
from formatting import trim_trailing_empty
from telegram_auth import validate_init_data


class GroupBody(BaseModel):
    telegram_id: int
    group: str
    program: Optional[str] = None
    course: Optional[int] = None


class VisitBody(BaseModel):
    init_data: str


def build_day_slots(grid, day_name):
    subjects = [grid[day_name][t] for t in TIME_SLOTS]
    trimmed = trim_trailing_empty(subjects)
    practice = grid[day_name].get(PRACTICE_SLOT)
    if trimmed is None and not practice:
        return None
    slots = [{"time": t.replace(".", ":"), "subject": s} for t, s in zip(TIME_SLOTS, trimmed or [])]
    if practice:
        slots.append({"time": PRACTICE_SLOT, "subject": practice})
    return slots


def _group_json(group):
    return {"program": group.program, "course": group.course, "group": group.name} if group else None


def build_router(conn, bot_token) -> APIRouter:
    router = APIRouter(prefix="/api")

    def require_group(telegram_id):
        group = get_user_group(conn, telegram_id)
        if not group:
            raise HTTPException(status_code=404, detail="group_not_set")
        return group

    @router.get("/day")
    def get_day(telegram_id: int, date: str):
        group = require_group(telegram_id)
        try:
            target_date = datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="bad_date")

        week_sheet, lessons = get_day_lessons(conn, group, target_date)
        if not week_sheet:
            raise HTTPException(status_code=404, detail="week_not_found")

        return {
            "group": group.name,
            "program": group.program,
            "week_sheet": week_sheet,
            "day_name": WEEKDAY_MAP.get(target_date.weekday(), "Воскресенье"),
            "date": date,
            "lessons": lessons,
        }

    @router.get("/schedule")
    def get_schedule(telegram_id: int, offset: int = 0):
        group = require_group(telegram_id)
        week_sheet = get_relative_week_sheet(conn, group, offset)
        if not week_sheet:
            raise HTTPException(status_code=404, detail="week_not_found")

        grid = get_week_schedule_grid(conn, group, week_sheet)
        days_out = []
        for day_name in DAYS_ORDER:
            slots = build_day_slots(grid, day_name)
            if slots:
                days_out.append({"day": day_name, "slots": slots})
        return {"group": group.name, "program": group.program, "week_sheet": week_sheet, "days": days_out}

    @router.get("/catalog")
    def catalog(telegram_id: int = 0):
        """Направления -> курсы -> группы и текущий выбор пользователя."""
        return {"programs": get_catalog(conn), "current": _group_json(get_user_group(conn, telegram_id))}

    @router.get("/groups")
    def list_groups(telegram_id: int = 0):
        # старый вариант списка (его вызывают закешированные версии мини-приложения)
        current = get_user_group(conn, telegram_id)
        groups = get_groups(conn, current.program, current.course) if current else get_groups(conn)
        return {"groups": [g.name for g in groups], "current": current.name if current else None}

    @router.post("/group")
    def choose_group(body: GroupBody):
        if body.program and body.course is not None:
            group = GroupRef(body.program, body.course, body.group)
            if group not in get_groups(conn):
                raise HTTPException(status_code=400, detail="unknown_group")
        else:
            # старый вариант без направления: ищем по названию, предпочитая направление пользователя
            current = get_user_group(conn, body.telegram_id)
            matches = [g for g in get_groups(conn) if g.name == body.group]
            if not matches:
                raise HTTPException(status_code=400, detail="unknown_group")
            group = next((g for g in matches if current and (g.program, g.course) == (current.program, current.course)),
                         matches[0])
        set_user_group(conn, body.telegram_id, group)
        return _group_json(group)

    @router.post("/visit")
    def visit(body: VisitBody):
        """Мини-апп сообщает о заходе: по подписанным данным Telegram запоминаем username и время.
        Большинство пользователей после выбора группы пишут боту редко и открывают только мини-апп."""
        user = validate_init_data(body.init_data, bot_token)
        if not user:
            raise HTTPException(status_code=403, detail="bad_init_data")
        touch_user(conn, user["id"], user.get("username"),
                   " ".join(filter(None, [user.get("first_name"), user.get("last_name")])) or None)
        return {"ok": True}

    return router
