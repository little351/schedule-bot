import sqlite3
from datetime import datetime

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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ratings (
            subject_key TEXT,
            telegram_id INTEGER,
            option TEXT,
            PRIMARY KEY (subject_key, telegram_id)
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
    conn.execute(
        "INSERT OR REPLACE INTO users (telegram_id, group_name) VALUES (?, ?)",
        (telegram_id, group_name)
    )
    conn.commit()


def get_user_group(conn, telegram_id):
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


def get_current_week_sheet(conn):
    rows = conn.execute(
        "SELECT week_sheet, MIN(date) FROM lessons GROUP BY week_sheet"
    ).fetchall()

    today = datetime.now()
    candidates = []
    for week_sheet, min_date in rows:
        if not min_date:
            continue
        week_start = datetime.fromisoformat(min_date)
        candidates.append((week_start, week_sheet))

    candidates.sort()

    current = None
    for week_start, week_sheet in candidates:
        if week_start <= today:
            current = week_sheet
        else:
            break

    return current or (candidates[0][1] if candidates else None)


def get_today_day_name():
    return WEEKDAY_MAP[datetime.now().weekday()]


RATING_OPTIONS = ["attend", "confused", "interesting", "skip"]
RATING_LABELS = {
    "attend": "Ходить",
    "confused": "Пока не понял",
    "interesting": "Интересный",
    "skip": "Можно не ходить",
}
RATING_COLORS = {
    "attend": "#ff3b30",
    "confused": "#ffcc00",
    "interesting": "#34c759",
    "skip": "#8e8e93",
}


def set_rating(conn, subject_key, telegram_id, option):
    if option not in RATING_OPTIONS:
        raise ValueError("bad option")
    conn.execute(
        "INSERT OR REPLACE INTO ratings (subject_key, telegram_id, option) VALUES (?, ?, ?)",
        (subject_key, telegram_id, option)
    )
    conn.commit()


def get_rating_summary(conn, subject_key):
    rows = conn.execute(
        "SELECT option, COUNT(*) FROM ratings WHERE subject_key=? GROUP BY option",
        (subject_key,)
    ).fetchall()
    counts = {opt: 0 for opt in RATING_OPTIONS}
    for option, cnt in rows:
        if option in counts:
            counts[option] = cnt
    total = sum(counts.values())
    dominant = max(counts, key=counts.get) if total > 0 else None
    return {"counts": counts, "total": total, "dominant": dominant}


def get_user_rating(conn, subject_key, telegram_id):
    row = conn.execute(
        "SELECT option FROM ratings WHERE subject_key=? AND telegram_id=?",
        (subject_key, telegram_id)
    ).fetchone()
    return row[0] if row else None


def get_day_lessons(conn, group_name, target_date):
    week_sheet = get_week_sheet_for_date(conn, target_date)
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


def get_all_week_sheets_sorted(conn):
    rows = conn.execute(
        "SELECT week_sheet, MIN(date) FROM lessons GROUP BY week_sheet"
    ).fetchall()
    candidates = []
    for week_sheet, min_date in rows:
        if not min_date:
            continue
        week_start = datetime.fromisoformat(min_date)
        candidates.append((week_start, week_sheet))
    candidates.sort()
    return candidates


def get_week_sheet_for_date(conn, target_date):
    candidates = get_all_week_sheets_sorted(conn)
    if not candidates:
        return None
    result = None
    for week_start, week_sheet in candidates:
        if week_start <= target_date:
            result = week_sheet
        else:
            break
    return result or candidates[0][1]


def get_relative_week_sheet(conn, offset):
    candidates = get_all_week_sheets_sorted(conn)
    current = get_current_week_sheet(conn)
    names = [c[1] for c in candidates]
    if current not in names:
        return None
    idx = names.index(current)
    new_idx = idx + offset
    if 0 <= new_idx < len(names):
        return names[new_idx]
    return None