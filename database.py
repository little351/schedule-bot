import os
import re
import sqlite3
from datetime import datetime

PG_DSN = os.environ.get("DATABASE_URL")
pg_conn = None
if PG_DSN:
    import psycopg2
    pg_conn = psycopg2.connect(PG_DSN)
    pg_conn.autocommit = True
    with pg_conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id BIGINT PRIMARY KEY,
                group_name TEXT
            )
        """)

DAYS_ORDER = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота"]
TIME_SLOTS = ["8.50-10.20", "10.40-12.10", "13.00-14.30", "14.50-16.20", "16.40-18.10"]

WEEKDAY_MAP = {
    0: "Понедельник", 1: "Вторник", 2: "Среда",
    3: "Четверг", 4: "Пятница", 5: "Суббота", 6: "Воскресенье",
}


def init_db(path="schedule.db"):
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lessons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            week_sheet TEXT, day TEXT, date TEXT,
            time TEXT, group_name TEXT, subject TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            group_name TEXT
        )
    """)
    conn.commit()
    return conn


def save_schedule(conn, records):
    conn.execute("DELETE FROM lessons")
    conn.executemany(
        "INSERT INTO lessons (week_sheet, day, date, time, group_name, subject) VALUES (?,?,?,?,?,?)",
        [(r["week_sheet"], r["day"], str(r["date"]), r["time"], r["group"], r["subject"]) for r in records]
    )
    conn.commit()


def set_user_group(conn, telegram_id, group_name):
    if pg_conn:
        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (telegram_id, group_name) VALUES (%s, %s) "
                "ON CONFLICT (telegram_id) DO UPDATE SET group_name = EXCLUDED.group_name",
                (telegram_id, group_name)
            )
        return
    conn.execute(
        "INSERT OR REPLACE INTO users (telegram_id, group_name) VALUES (?, ?)",
        (telegram_id, group_name)
    )
    conn.commit()


def get_user_group(conn, telegram_id):
    if pg_conn:
        with pg_conn.cursor() as cur:
            cur.execute("SELECT group_name FROM users WHERE telegram_id=%s", (telegram_id,))
            row = cur.fetchone()
        return row[0] if row else None
    row = conn.execute(
        "SELECT group_name FROM users WHERE telegram_id = ?", (telegram_id,)
    ).fetchone()
    return row[0] if row else None


def get_week_schedule_grid(conn, group_name, week_sheet):
    rows = conn.execute(
        "SELECT day, time, subject FROM lessons WHERE group_name=? AND week_sheet=?",
        (group_name, week_sheet)
    ).fetchall()
    grid = {day: {t: None for t in TIME_SLOTS} for day in DAYS_ORDER}
    for day, time, subject in rows:
        if day in grid and time in grid[day]:
            grid[day][time] = subject
    return grid


def get_current_week_sheet(conn, group_name):
    return get_week_sheet_for_date(conn, group_name, datetime.now())


def get_today_day_name():
    return WEEKDAY_MAP[datetime.now().weekday()]


def get_day_lessons(conn, group_name, target_date):
    week_sheet = get_week_sheet_for_date(conn, group_name, target_date)
    if not week_sheet:
        return None, []

    weekday = target_date.weekday()
    day_name = WEEKDAY_MAP.get(weekday)
    if day_name not in DAYS_ORDER:
        return week_sheet, []  # воскресенье - пар не бывает

    grid = get_week_schedule_grid(conn, group_name, week_sheet)
    lessons = []
    for t in TIME_SLOTS:
        subj = grid[day_name][t]
        if subj:
            start, end = t.split("-")
            lessons.append({
                "time_start": start.replace(".", ":"),
                "time_end": end.replace(".", ":"),
                "subject": subj,
            })
    return week_sheet, lessons


def get_all_week_sheets_sorted(conn, group_name):
    # у разных групп (из разных файлов) набор недель может отличаться
    rows = conn.execute(
        "SELECT week_sheet, MIN(date) FROM lessons WHERE group_name=? GROUP BY week_sheet",
        (group_name,)
    ).fetchall()
    candidates = []
    for week_sheet, min_date in rows:
        if not min_date or min_date == "None":
            continue
        week_start = datetime.fromisoformat(min_date)
        candidates.append((week_start, week_sheet))
    candidates.sort()
    return candidates


def get_week_sheet_for_date(conn, group_name, target_date):
    candidates = get_all_week_sheets_sorted(conn, group_name)
    if not candidates:
        return None
    result = None
    for week_start, week_sheet in candidates:
        if week_start <= target_date:
            result = week_sheet
        else:
            break
    return result or candidates[0][1]


def get_relative_week_sheet(conn, group_name, offset):
    candidates = get_all_week_sheets_sorted(conn, group_name)
    current = get_current_week_sheet(conn, group_name)
    names = [c[1] for c in candidates]
    if current not in names:
        return None
    idx = names.index(current)
    new_idx = idx + offset
    if 0 <= new_idx < len(names):
        return names[new_idx]
    return None


def _group_sort_key(name):
    m = re.match(r"\d+", name)
    return (int(m.group()) if m else float("inf"), name)


def get_groups(conn):
    """Все группы, для которых загружено расписание: ['10 группа', '11 группа', ...]"""
    rows = conn.execute("SELECT DISTINCT group_name FROM lessons").fetchall()
    return sorted((r[0] for r in rows), key=_group_sort_key)


def group_short_name(group_name):
    """'10 группа' -> '10' (то, что пользователь пишет боту)."""
    return re.sub(r"\s*группа\s*$", "", group_name, flags=re.IGNORECASE).strip()


def find_group(conn, text):
    """Ищет группу по вводу пользователя: '10', '10 группа', '10 ГРУППА'."""
    needle = re.sub(r"\s+", " ", text).strip().lower()
    for group in get_groups(conn):
        if needle in (group.lower(), group_short_name(group).lower()):
            return group
    return None
