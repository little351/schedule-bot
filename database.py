import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

# колонки таблицы users, добавленные после первой версии (миграция идёт при старте)
USER_EXTRA_COLUMNS = [
    ("username", "TEXT"), ("first_name", "TEXT"), ("first_seen", "TEXT"), ("last_seen", "TEXT"),
    ("program", "TEXT"), ("course", "INTEGER"),  # направление и курс выбранной группы
]
# до появления направлений все группы были с 1 курса направления 49.03.04
LEGACY_PROGRAM = "49.03.04"
LEGACY_COURSE = 1

PG_DSN = os.environ.get("DATABASE_URL")
pg_conn = None


def _pg_connect():
    """(Пере)подключение к Postgres. Бесплатные облачные базы (Neon и др.) закрывают простаивающие
    соединения, поэтому живое соединение проверяется перед запросом и при необходимости открывается заново."""
    global pg_conn
    import psycopg2
    pg_conn = psycopg2.connect(PG_DSN, keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3)
    pg_conn.autocommit = True
    return pg_conn


@contextmanager
def pg_cursor():
    import psycopg2
    for attempt in (1, 2):
        try:
            if pg_conn is None or pg_conn.closed:
                _pg_connect()
            cur = pg_conn.cursor()
            cur.execute("SELECT 1")  # соединение могло быть закрыто сервером, пока бот простаивал
            break
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            if attempt == 2:
                raise
            _pg_connect()
    try:
        yield cur
    finally:
        cur.close()


if PG_DSN:
    _pg_connect()
    with pg_cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id BIGINT PRIMARY KEY,
                group_name TEXT
            )
        """)
        # учёт пользователей: колонки добавляются к уже существующей таблице без потери данных
        for column, column_type in USER_EXTRA_COLUMNS:
            cur.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {column} {column_type}")
        # у пользователей с направлением курс по умолчанию первый
        cur.execute("UPDATE users SET course = 1 WHERE program IS NOT NULL AND course IS NULL")


def storage_name():
    """Где лежат пользователи: Postgres (переживает деплой) или локальная SQLite (стирается при деплое)."""
    return "Postgres" if PG_DSN else "локальная SQLite (данные стираются при деплое)"


DAYS_ORDER = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота"]
TIME_SLOTS = ["8.50-10.20", "10.40-12.10", "13.00-14.30", "14.50-16.20", "16.40-18.10", "18.30-20.00"]

WEEKDAY_MAP = {
    0: "Понедельник", 1: "Вторник", 2: "Среда",
    3: "Четверг", 4: "Пятница", 5: "Суббота", 6: "Воскресенье",
}


class GroupRef(NamedTuple):
    """Группа однозначно определяется направлением, курсом и названием:
    номера групп могут повторяться на разных направлениях и курсах."""
    program: str   # код направления, например "49.03.04"
    course: int
    name: str      # как в файле: "13 группа", "Т2-09-26"

    @property
    def title(self):
        return f"{self.name} ({self.program}, {self.course} курс)"

    @property
    def short_name(self):
        """'10 группа' -> '10' (то, что пользователь пишет боту)."""
        return re.sub(r"\s*группа\s*$", "", self.name, flags=re.IGNORECASE).strip()


def init_db(path="schedule.db"):
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lessons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            week_sheet TEXT, day TEXT, date TEXT,
            time TEXT, group_name TEXT, subject TEXT,
            program TEXT, course INTEGER
        )
    """)
    conn.execute("CREATE TABLE IF NOT EXISTS programs (code TEXT PRIMARY KEY, name TEXT)")
    _add_missing_columns(conn, "lessons", [("program", "TEXT"), ("course", "INTEGER")])
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            group_name TEXT
        )
    """)
    _add_missing_columns(conn, "users", USER_EXTRA_COLUMNS)
    conn.execute("UPDATE users SET course = 1 WHERE program IS NOT NULL AND course IS NULL")
    conn.commit()
    return conn


def _add_missing_columns(conn, table, columns):
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for column, column_type in columns:
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def save_schedule(conn, records):
    conn.execute("DELETE FROM lessons")
    conn.execute("DELETE FROM programs")
    conn.executemany(
        "INSERT INTO lessons (week_sheet, day, date, time, group_name, subject, program, course) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [(r["week_sheet"], r["day"], str(r["date"]), r["time"], r["group"], r["subject"],
          r["program"], r["course"]) for r in records]
    )
    programs = {r["program"]: r.get("program_name") or r["program"] for r in records}
    conn.executemany("INSERT INTO programs (code, name) VALUES (?, ?)", sorted(programs.items()))
    conn.commit()


def set_user_group(conn, telegram_id, group: GroupRef):
    if PG_DSN:
        with pg_cursor() as cur:
            cur.execute(
                "INSERT INTO users (telegram_id, group_name, program, course) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (telegram_id) DO UPDATE SET group_name = EXCLUDED.group_name, "
                "program = EXCLUDED.program, course = EXCLUDED.course",
                (telegram_id, group.name, group.program, group.course)
            )
        return
    conn.execute(
        "INSERT INTO users (telegram_id, group_name, program, course) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (telegram_id) DO UPDATE SET group_name = excluded.group_name, "
        "program = excluded.program, course = excluded.course",
        (telegram_id, group.name, group.program, group.course)
    )
    conn.commit()


def get_user_group(conn, telegram_id):
    """Выбранная группа пользователя (GroupRef) или None.

    Пользователи, выбравшие группу до появления направлений, хранятся только с названием группы:
    для них направление и курс находятся по загруженному расписанию и записываются в базу."""
    if PG_DSN:
        with pg_cursor() as cur:
            cur.execute("SELECT group_name, program, course FROM users WHERE telegram_id=%s", (telegram_id,))
            row = cur.fetchone()
    else:
        row = conn.execute(
            "SELECT group_name, program, course FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
    if not row or not row[0]:
        return None
    name, program, course = row
    if program and course:
        return GroupRef(program, int(course), name)

    group, found = _resolve_legacy_group(conn, name)
    if found:
        set_user_group(conn, telegram_id, group)
    return group


def _resolve_legacy_group(conn, name):
    """(GroupRef, найдена ли группа в расписании) для группы, выбранной до появления направлений."""
    rows = conn.execute(
        "SELECT DISTINCT program, course FROM lessons WHERE group_name = ? ORDER BY course, program",
        (name,)
    ).fetchall()
    rows = [r for r in rows if r[0]]
    if not rows:
        return GroupRef(LEGACY_PROGRAM, LEGACY_COURSE, name), False
    preferred = next((r for r in rows if r == (LEGACY_PROGRAM, LEGACY_COURSE)), rows[0])
    return GroupRef(preferred[0], int(preferred[1]), name), True


def touch_user(conn, telegram_id, username, first_name):
    """Запоминает пользователя при любом обращении: username (может быть пустым),
    имя, время первого и последнего обращения. Группу не трогает."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if PG_DSN:
        with pg_cursor() as cur:
            cur.execute(
                "INSERT INTO users (telegram_id, username, first_name, first_seen, last_seen) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (telegram_id) DO UPDATE SET username = EXCLUDED.username, "
                "first_name = EXCLUDED.first_name, last_seen = EXCLUDED.last_seen, "
                "first_seen = COALESCE(users.first_seen, EXCLUDED.first_seen)",
                (telegram_id, username, first_name, now, now)
            )
        return
    conn.execute(
        "INSERT INTO users (telegram_id, username, first_name, first_seen, last_seen) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (telegram_id) DO UPDATE SET username = excluded.username, "
        "first_name = excluded.first_name, last_seen = excluded.last_seen, "
        "first_seen = COALESCE(users.first_seen, excluded.first_seen)",
        (telegram_id, username, first_name, now, now)
    )
    conn.commit()


def users_without_profile(conn, limit=50):
    """ID пользователей, о которых бот ещё не знает имени (записи до появления учёта)."""
    if PG_DSN:
        with pg_cursor() as cur:
            cur.execute("SELECT telegram_id FROM users WHERE first_name IS NULL LIMIT %s", (limit,))
            return [r[0] for r in cur.fetchall()]
    rows = conn.execute("SELECT telegram_id FROM users WHERE first_name IS NULL LIMIT ?", (limit,)).fetchall()
    return [r[0] for r in rows]


def set_user_profile(conn, telegram_id, username, first_name):
    """Обновляет только username и имя, не трогая время обращений и группу."""
    if PG_DSN:
        with pg_cursor() as cur:
            cur.execute(
                "UPDATE users SET username = %s, first_name = %s WHERE telegram_id = %s",
                (username, first_name, telegram_id)
            )
        return
    conn.execute(
        "UPDATE users SET username = ?, first_name = ? WHERE telegram_id = ?",
        (username, first_name, telegram_id)
    )
    conn.commit()


def restore_users(conn, rows):
    """Добавляет пользователей из резервной копии: [(telegram_id, username, first_name, group_name,
    first_seen, last_seen, program, course), ...]. Уже существующие записи не трогает (они свежее копии).
    Возвращает, сколько пользователей добавлено."""
    added = 0
    for telegram_id, username, first_name, group_name, first_seen, last_seen, program, course in rows:
        params = (telegram_id, group_name, username, first_name, first_seen, last_seen, program, course)
        if PG_DSN:
            with pg_cursor() as cur:
                cur.execute(
                    "INSERT INTO users (telegram_id, group_name, username, first_name, first_seen, last_seen, program, course) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (telegram_id) DO NOTHING", params)
                added += cur.rowcount
        else:
            cur = conn.execute(
                "INSERT INTO users (telegram_id, group_name, username, first_name, first_seen, last_seen, program, course) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (telegram_id) DO NOTHING", params)
            added += cur.rowcount
    if not PG_DSN:
        conn.commit()
    return added


def list_users(conn):
    """Все пользователи, сначала те, кто обращался недавно:
    [(telegram_id, username, first_name, group_name, first_seen, last_seen, program, course), ...]"""
    query = (
        "SELECT telegram_id, username, first_name, group_name, first_seen, last_seen, program, course "
        "FROM users ORDER BY last_seen IS NULL, last_seen DESC, telegram_id"
    )
    if PG_DSN:
        with pg_cursor() as cur:
            cur.execute(query.replace("last_seen IS NULL, last_seen DESC", "last_seen DESC NULLS LAST"))
            return cur.fetchall()
    return conn.execute(query).fetchall()


def get_week_schedule_grid(conn, group: GroupRef, week_sheet):
    rows = conn.execute(
        "SELECT day, time, subject FROM lessons "
        "WHERE program=? AND course=? AND group_name=? AND week_sheet=?",
        (*group, week_sheet)
    ).fetchall()
    grid = {day: {t: None for t in TIME_SLOTS} for day in DAYS_ORDER}
    for day, time, subject in rows:
        if day in grid and time in grid[day]:
            grid[day][time] = subject
    return grid


def get_current_week_sheet(conn, group: GroupRef):
    return get_week_sheet_for_date(conn, group, datetime.now())


def get_today_day_name():
    return WEEKDAY_MAP[datetime.now().weekday()]


def get_day_lessons(conn, group: GroupRef, target_date):
    week_sheet = get_week_sheet_for_date(conn, group, target_date)
    if not week_sheet:
        return None, []

    weekday = target_date.weekday()
    day_name = WEEKDAY_MAP.get(weekday)
    if day_name not in DAYS_ORDER:
        return week_sheet, []  # воскресенье - пар не бывает

    grid = get_week_schedule_grid(conn, group, week_sheet)
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


def get_all_week_sheets_sorted(conn, group: GroupRef):
    # у разных групп (из разных файлов) набор недель может отличаться
    rows = conn.execute(
        "SELECT week_sheet, MIN(date) FROM lessons "
        "WHERE program=? AND course=? AND group_name=? GROUP BY week_sheet",
        tuple(group)
    ).fetchall()
    candidates = []
    for week_sheet, min_date in rows:
        if not min_date or min_date == "None":
            continue
        first_lesson = datetime.fromisoformat(min_date)
        # неделя начинается в понедельник, даже если у группы в этот день нет пар
        week_start = first_lesson - timedelta(days=first_lesson.weekday())
        candidates.append((week_start, week_sheet))
    candidates.sort()
    return candidates


def get_week_sheet_for_date(conn, group: GroupRef, target_date):
    candidates = get_all_week_sheets_sorted(conn, group)
    if not candidates:
        return None
    result = None
    for week_start, week_sheet in candidates:
        if week_start <= target_date:
            result = week_sheet
        else:
            break
    return result or candidates[0][1]


def get_relative_week_sheet(conn, group: GroupRef, offset):
    candidates = get_all_week_sheets_sorted(conn, group)
    current = get_current_week_sheet(conn, group)
    names = [c[1] for c in candidates]
    if current not in names:
        return None
    idx = names.index(current)
    new_idx = idx + offset
    if 0 <= new_idx < len(names):
        return names[new_idx]
    return None


def _group_sort_key(group: GroupRef):
    m = re.match(r"\d+", group.name)
    return (group.program, group.course, int(m.group()) if m else float("inf"), group.name)


def get_groups(conn, program=None, course=None):
    """Группы, для которых загружено расписание, как список GroupRef (можно сузить по направлению и курсу)."""
    query = "SELECT DISTINCT program, course, group_name FROM lessons WHERE program IS NOT NULL"
    params = []
    if program is not None:
        query += " AND program = ?"
        params.append(program)
    if course is not None:
        query += " AND course = ?"
        params.append(course)
    return sorted((GroupRef(p, int(c), n) for p, c, n in conn.execute(query, params)), key=_group_sort_key)


def get_catalog(conn):
    """Направления -> курсы -> группы:
    [{"code", "name", "courses": [{"course": 1, "groups": ["1 группа", ...]}]}, ...]"""
    names = dict(conn.execute("SELECT code, name FROM programs"))
    catalog = {}
    for g in get_groups(conn):
        program = catalog.setdefault(g.program, {"code": g.program, "name": names.get(g.program, g.program), "courses": {}})
        program["courses"].setdefault(g.course, []).append(g.name)
    return [
        {"code": p["code"], "name": p["name"],
         "courses": [{"course": c, "groups": groups} for c, groups in sorted(p["courses"].items())]}
        for p in sorted(catalog.values(), key=lambda p: p["code"])
    ]


def group_short_name(group_name):
    return GroupRef("", 0, group_name).short_name


def find_groups(conn, text, program=None, course=None):
    """Группы по вводу пользователя ('10', '10 группа', 'Т2-09-26'); может быть несколько,
    если номер совпадает на разных направлениях или курсах."""
    needle = re.sub(r"\s+", " ", text).strip().lower()
    return [
        g for g in get_groups(conn, program, course)
        if needle in (g.name.lower(), g.short_name.lower())
    ]


def get_program_name(conn, code):
    row = conn.execute("SELECT name FROM programs WHERE code = ?", (code,)).fetchone()
    return row[0] if row else code
