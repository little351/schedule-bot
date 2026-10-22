"""HTTP API мини-приложения. Используется и боевым сервером (main.py), и локальным (api.py)."""
import json
import os
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from database import (
    DAYS_ORDER, PRACTICE_SLOT, TIME_SLOTS, WEEKDAY_MAP, GroupRef, get_catalog, get_day_lessons,
    get_groups, get_relative_week_sheet, get_user_group, get_week_schedule_grid, set_user_group,
    touch_user, NOTES_LIMIT, add_note, count_notes, delete_note, get_group_subjects, list_notes, update_note,
    game_leaderboard, save_game_score,
)
from formatting import trim_trailing_empty
from telegram_auth import MAX_AGE_SECONDS, READ_MAX_AGE_SECONDS, validate_init_data


TEACHERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "teachers.json")


def load_teachers(path=TEACHERS_FILE):
    """Кафедры и преподаватели из data/teachers.json; нет файла или он испорчен - пустой список."""
    try:
        with open(path, encoding="utf-8") as f:
            departments = json.load(f).get("departments", [])
    except (OSError, ValueError, AttributeError):
        return []
    return [d for d in departments if isinstance(d, dict) and d.get("name")]


class GroupBody(BaseModel):
    telegram_id: int = 0  # учитывается только без токена бота (локальный api.py)
    group: str
    program: Optional[str] = None
    course: Optional[int] = None


class VisitBody(BaseModel):
    init_data: str


class NoteBody(BaseModel):
    subject: str = ""
    text: str = ""
    due_date: Optional[str] = None  # ГГГГ-ММ-ДД


class GameScore(BaseModel):
    score: int


# Потолок счёта партии: после диплома игра бесконечная, но столько честно не набрать - всё сверху подделка запроса.
GAME_SCORE_MAX = 1_000_000


class NotePatch(BaseModel):
    subject: Optional[str] = None
    text: Optional[str] = None
    due_date: Optional[str] = None  # пустая строка убирает дедлайн
    done: Optional[bool] = None


def _clean_note_fields(subject=None, text=None, due_date=None):
    """Проверка и очистка полей заметки; возвращает только переданные поля."""
    out = {}
    if subject is not None:
        out["subject"] = subject.strip()[:100]
    if text is not None:
        out["text"] = text.strip()[:500]
    if due_date is not None:
        due_date = due_date.strip()
        if due_date:
            try:
                datetime.strptime(due_date, "%Y-%m-%d")
            except ValueError:
                raise HTTPException(status_code=400, detail="bad_date")
        out["due_date"] = due_date or None
    return out


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

    def request_user(init_data, telegram_id, required=True, max_age=MAX_AGE_SECONDS):
        """Кто спрашивает: id из подписанных данных Telegram (заголовок X-Init-Data), чтобы нельзя было
        подставить чужой id. Без токена бота (локальный api.py) берётся telegram_id из запроса."""
        if not bot_token:
            return telegram_id
        user = validate_init_data(init_data or "", bot_token, max_age)
        if user:
            return user["id"]
        if required:
            raise HTTPException(status_code=403, detail="bad_init_data")
        return 0

    def require_group(telegram_id):
        group = get_user_group(conn, telegram_id)
        if not group:
            raise HTTPException(status_code=404, detail="group_not_set")
        return group

    @router.get("/day")
    def get_day(date: str, telegram_id: int = 0, x_init_data: str = Header(default="")):
        group = require_group(request_user(x_init_data, telegram_id, max_age=READ_MAX_AGE_SECONDS))
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
    def get_schedule(telegram_id: int = 0, offset: int = 0, x_init_data: str = Header(default="")):
        group = require_group(request_user(x_init_data, telegram_id, max_age=READ_MAX_AGE_SECONDS))
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

    @router.get("/teachers")
    def teachers():
        return {"departments": load_teachers()}

    @router.get("/catalog")
    def catalog(telegram_id: int = 0, x_init_data: str = Header(default="")):
        """Направления -> курсы -> группы и текущий выбор пользователя (без подписи - только список)."""
        user_id = request_user(x_init_data, telegram_id, required=False, max_age=READ_MAX_AGE_SECONDS)
        return {"programs": get_catalog(conn), "current": _group_json(get_user_group(conn, user_id) if user_id else None)}

    @router.get("/groups")
    def list_groups(telegram_id: int = 0, x_init_data: str = Header(default="")):
        # старый вариант списка (его вызывают закешированные версии мини-приложения)
        user_id = request_user(x_init_data, telegram_id, required=False, max_age=READ_MAX_AGE_SECONDS)
        current = get_user_group(conn, user_id) if user_id else None
        groups = get_groups(conn, current.program, current.course) if current else get_groups(conn)
        return {"groups": [g.name for g in groups], "current": current.name if current else None}

    @router.post("/group")
    def choose_group(body: GroupBody, x_init_data: str = Header(default="")):
        user_id = request_user(x_init_data, body.telegram_id)
        if body.program and body.course is not None:
            group = GroupRef(body.program, body.course, body.group)
            if group not in get_groups(conn):
                raise HTTPException(status_code=400, detail="unknown_group")
        else:
            # старый вариант без направления: ищем по названию, предпочитая направление пользователя
            current = get_user_group(conn, user_id)
            matches = [g for g in get_groups(conn) if g.name == body.group]
            if not matches:
                raise HTTPException(status_code=400, detail="unknown_group")
            group = next((g for g in matches if current and (g.program, g.course) == (current.program, current.course)),
                         matches[0])
        set_user_group(conn, user_id, group)
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

    def note_user(init_data):
        """Заметки видит и меняет только сам пользователь: личность берётся из подписанных данных Telegram."""
        user = validate_init_data(init_data or "", bot_token)
        if not user:
            raise HTTPException(status_code=403, detail="bad_init_data")
        return user["id"]

    @router.get("/notes")
    def notes(x_init_data: str = Header(default="")):
        user_id = note_user(x_init_data)
        group = get_user_group(conn, user_id)
        return {"notes": list_notes(conn, user_id), "subjects": get_group_subjects(conn, group) if group else []}

    @router.post("/notes")
    def create_note(body: NoteBody, x_init_data: str = Header(default="")):
        user_id = note_user(x_init_data)
        fields = _clean_note_fields(body.subject, body.text, body.due_date)
        fields.setdefault("due_date", None)
        if not fields["subject"] and not fields["text"]:
            raise HTTPException(status_code=400, detail="empty_note")
        if count_notes(conn, user_id) >= NOTES_LIMIT:
            raise HTTPException(status_code=400, detail="too_many_notes")
        add_note(conn, user_id, fields["subject"], fields["text"], fields["due_date"])
        return {"notes": list_notes(conn, user_id)}

    @router.patch("/notes/{note_id}")
    def change_note(note_id: int, body: NotePatch, x_init_data: str = Header(default="")):
        user_id = note_user(x_init_data)
        fields = _clean_note_fields(body.subject, body.text, body.due_date)
        if body.done is not None:
            fields["done"] = 1 if body.done else 0
        if not update_note(conn, user_id, note_id, **fields):
            raise HTTPException(status_code=404, detail="note_not_found")
        return {"notes": list_notes(conn, user_id)}

    @router.delete("/notes/{note_id}")
    def remove_note(note_id: int, x_init_data: str = Header(default="")):
        user_id = note_user(x_init_data)
        if not delete_note(conn, user_id, note_id):
            raise HTTPException(status_code=404, detail="note_not_found")
        return {"notes": list_notes(conn, user_id)}

    @router.get("/game/top")
    def game_top(x_init_data: str = Header(default="")):
        user = validate_init_data(x_init_data or "", bot_token, READ_MAX_AGE_SECONDS)
        return game_leaderboard(conn, user["id"] if user else 0)

    @router.post("/game/score")
    def game_score(body: GameScore, x_init_data: str = Header(default="")):
        """Конец партии мини-игры: запоминаем рекорд и сразу отдаём таблицу лидеров (один запрос на партию)."""
        user = validate_init_data(x_init_data or "", bot_token)
        if not user:
            raise HTTPException(status_code=403, detail="bad_init_data")
        if not 0 <= body.score <= GAME_SCORE_MAX:
            raise HTTPException(status_code=400, detail="bad_score")
        last = (user.get("last_name") or "").strip()
        name = ((user.get("first_name") or "").strip() + (f" {last[0]}." if last else "")).strip()
        if body.score > 0:
            save_game_score(conn, user["id"], name[:20] or "Игрок", body.score)
        return game_leaderboard(conn, user["id"])

    return router
