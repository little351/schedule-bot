import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("Europe/Moscow")
except Exception:  # нет базы часовых поясов в системе: Москва - это UTC+3 без перехода на летнее время
    LOCAL_TZ = timezone(timedelta(hours=3))


def now_local():
    """Текущее время по Москве (сервер живёт в UTC, а «сегодня» для студентов - московское)."""
    return datetime.now(LOCAL_TZ).replace(tzinfo=None)

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
        cur.execute("""
            CREATE TABLE IF NOT EXISTS synced_files (
                key TEXT PRIMARY KEY, file_name TEXT, content BYTEA, sha256 TEXT,
                fetched_at TEXT, group_counts TEXT, records INTEGER
            )
        """)
        cur.execute("CREATE TABLE IF NOT EXISTS sync_state (key TEXT PRIMARY KEY, value TEXT)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS notes (
                id BIGSERIAL PRIMARY KEY, telegram_id BIGINT NOT NULL, subject TEXT, text TEXT,
                due_date TEXT, done INTEGER NOT NULL DEFAULT 0, created_at TEXT
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS notes_user ON notes (telegram_id)")


def storage_name():
    """Где лежат пользователи: Postgres (переживает деплой) или локальная SQLite (стирается при деплое)."""
    return "Postgres" if PG_DSN else "локальная SQLite (данные стираются при деплое)"


DAYS_ORDER = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота"]
# 7-я пара бывает у вечерних групп (очно-заочная форма, например Сестринское дело)
TIME_SLOTS = ["8.50-10.20", "10.40-12.10", "13.00-14.30", "14.50-16.20", "16.40-18.10", "18.30-20.00", "20.20-21.50"]
PRACTICE_SLOT = "Практика"  # запись на весь день вместо пары

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


class _Result:
    """Результат запроса, уже прочитанный целиком: его можно перебирать после того, как соединение освободилось."""

    def __init__(self, rows, rowcount, lastrowid):
        self._rows, self.rowcount, self.lastrowid = rows, rowcount, lastrowid

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


class LockedConnection:
    """Соединение SQLite для нескольких потоков: API мини-аппа обрабатывает запросы параллельно, а одно
    соединение sqlite3 одновременных запросов не выдерживает (пустые ответы и ошибки). Каждый запрос
    выполняется под замком и читается целиком; `with conn.lock:` держит замок на несколько запросов подряд."""

    def __init__(self, raw):
        self.raw = raw
        self.lock = threading.RLock()

    def execute(self, sql, params=()):
        with self.lock:
            cur = self.raw.execute(sql, params)
            return _Result(cur.fetchall() if cur.description else [], cur.rowcount, cur.lastrowid)

    def executemany(self, sql, seq):
        with self.lock:
            cur = self.raw.executemany(sql, seq)
            return _Result([], cur.rowcount, cur.lastrowid)

    def commit(self):
        with self.lock:
            self.raw.commit()

    def close(self):
        with self.lock:
            self.raw.close()


def init_db(path="schedule.db"):
    conn = LockedConnection(sqlite3.connect(path, check_same_thread=False))
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS synced_files (
            key TEXT PRIMARY KEY, file_name TEXT, content BLOB, sha256 TEXT,
            fetched_at TEXT, group_counts TEXT, records INTEGER
        )
    """)
    conn.execute("CREATE TABLE IF NOT EXISTS sync_state (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER NOT NULL, subject TEXT, text TEXT,
            due_date TEXT, done INTEGER NOT NULL DEFAULT 0, created_at TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS notes_user ON notes (telegram_id)")
    conn.commit()
    return conn


def _add_missing_columns(conn, table, columns):
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for column, column_type in columns:
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def save_schedule(conn, records):
    # под одним замком до commit: параллельный запрос не должен увидеть таблицу пустой или наполовину заполненной
    with conn.lock:
        _replace_schedule(conn, records)


def _replace_schedule(conn, records):
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


def user_stats(rows, now=None, top=10):
    """Счётчики по пользователям из list_users(): без имён и ID. now - aware datetime (по умолчанию сейчас, UTC).
    Даты first_seen/last_seen хранятся в UTC ISO; у записей до появления учёта и из резервной копии их может не быть."""
    now = now or datetime.now(timezone.utc)

    def parse(value):
        try:
            d = datetime.fromisoformat(value) if value else None
        except ValueError:
            return None
        return d.replace(tzinfo=timezone.utc) if d and d.tzinfo is None else d

    stats = {"total": len(rows), "no_group": 0, "no_first_seen": 0, "no_last_seen": 0,
             "new": {1: 0, 7: 0, 30: 0}, "active": {1: 0, 7: 0, 30: 0},
             "courses": {}, "programs": {}, "groups": {}}
    for _, _, _, group_name, first_seen, last_seen, program, course in rows:
        if not group_name:
            stats["no_group"] += 1
        else:
            stats["courses"][course] = stats["courses"].get(course, 0) + 1
            stats["programs"][program] = stats["programs"].get(program, 0) + 1
            key = (program, course, group_name)
            stats["groups"][key] = stats["groups"].get(key, 0) + 1
        first, last = parse(first_seen), parse(last_seen)
        stats["no_first_seen"] += first is None
        stats["no_last_seen"] += last is None
        for days in (1, 7, 30):
            limit = now - timedelta(days=days)
            stats["new"][days] += bool(first and first >= limit)
            stats["active"][days] += bool(last and last >= limit)
    stats["top_groups"] = sorted(stats["groups"].items(), key=lambda kv: (-kv[1], kv[0][0] or "", kv[0][2]))[:top]
    return stats


# --- файлы расписания, подгруженные с сайта вуза (lms_sync): последняя хорошая версия каждого файла ---

def save_synced_file(conn, key, file_name, content, sha256, fetched_at, group_counts, records):
    """Сохраняет (заменяет) файл с ключом key. group_counts - JSON {"программа|курс|группа": число занятий}."""
    if PG_DSN:
        import psycopg2
        with pg_cursor() as cur:
            cur.execute(
                "INSERT INTO synced_files (key, file_name, content, sha256, fetched_at, group_counts, records) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (key) DO UPDATE SET file_name = EXCLUDED.file_name, "
                "content = EXCLUDED.content, sha256 = EXCLUDED.sha256, fetched_at = EXCLUDED.fetched_at, "
                "group_counts = EXCLUDED.group_counts, records = EXCLUDED.records",
                (key, file_name, psycopg2.Binary(content), sha256, fetched_at, group_counts, records))
        return
    conn.execute(
        "INSERT INTO synced_files (key, file_name, content, sha256, fetched_at, group_counts, records) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (key) DO UPDATE SET file_name = excluded.file_name, "
        "content = excluded.content, sha256 = excluded.sha256, fetched_at = excluded.fetched_at, "
        "group_counts = excluded.group_counts, records = excluded.records",
        (key, file_name, content, sha256, fetched_at, group_counts, records))
    conn.commit()


def list_synced_files(conn, with_content=False):
    """[{key, file_name, sha256, fetched_at, groups, records[, content]}, ...] от старых к новым."""
    columns = "key, file_name, sha256, fetched_at, group_counts, records" + (", content" if with_content else "")
    query = f"SELECT {columns} FROM synced_files ORDER BY fetched_at, key"
    if PG_DSN:
        with pg_cursor() as cur:
            cur.execute(query)
            rows = cur.fetchall()
    else:
        rows = conn.execute(query).fetchall()
    names = ["key", "file_name", "sha256", "fetched_at", "group_counts", "records"] + (["content"] if with_content else [])
    result = []
    for row in rows:
        item = dict(zip(names, row))
        if with_content:
            item["content"] = bytes(item["content"])
        result.append(item)
    return result


def delete_synced_file(conn, key):
    if PG_DSN:
        with pg_cursor() as cur:
            cur.execute("DELETE FROM synced_files WHERE key = %s", (key,))
        return
    conn.execute("DELETE FROM synced_files WHERE key = ?", (key,))
    conn.commit()


def get_sync_state(conn, key, default=None):
    if PG_DSN:
        with pg_cursor() as cur:
            cur.execute("SELECT value FROM sync_state WHERE key = %s", (key,))
            row = cur.fetchone()
    else:
        row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_sync_state(conn, key, value):
    if PG_DSN:
        with pg_cursor() as cur:
            cur.execute("INSERT INTO sync_state (key, value) VALUES (%s, %s) "
                        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (key, value))
        return
    conn.execute("INSERT INTO sync_state (key, value) VALUES (?, ?) "
                 "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))
    conn.commit()


def get_week_schedule_grid(conn, group: GroupRef, week_sheet):
    rows = conn.execute(
        "SELECT day, time, subject FROM lessons "
        "WHERE program=? AND course=? AND group_name=? AND week_sheet=?",
        (*group, week_sheet)
    ).fetchall()
    grid = {day: {t: None for t in TIME_SLOTS + [PRACTICE_SLOT]} for day in DAYS_ORDER}
    for day, time, subject in rows:
        if day in grid and (time in TIME_SLOTS or time == PRACTICE_SLOT):
            grid[day][time] = subject
    return grid


def get_current_week_sheet(conn, group: GroupRef):
    return get_week_sheet_for_date(conn, group, now_local())


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
    if grid[day_name][PRACTICE_SLOT]:
        lessons.append({"time_start": PRACTICE_SLOT, "time_end": "", "subject": grid[day_name][PRACTICE_SLOT]})
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
    """Лист недели, в которую попадает дата (понедельник-воскресенье), или None, если в таблицах
    этой недели нет: раньше такая дата получала ближайшую прошлую неделю и показывала чужие пары."""
    for week_start, week_sheet in get_all_week_sheets_sorted(conn, group):
        if week_start <= target_date < week_start + timedelta(days=7):
            return week_sheet
    return None


def get_relative_week_sheet(conn, group: GroupRef, offset):
    return get_week_sheet_for_date(conn, group, now_local() + timedelta(days=7 * offset))


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


# ---------- заметки и дедлайны по предметам (хранятся по id пользователя) ----------

NOTES_LIMIT = 300  # заметок на одного пользователя
NOTE_FIELDS = "id, subject, text, due_date, done, created_at"


def _note_dict(row):
    return {"id": row[0], "subject": row[1] or "", "text": row[2] or "", "due_date": row[3] or None,
            "done": bool(row[4]), "created_at": row[5]}


def list_notes(conn, telegram_id):
    """Заметки пользователя: сначала несделанные, по дате дедлайна (без даты в конце), потом сделанные."""
    if PG_DSN:
        with pg_cursor() as cur:
            cur.execute(f"SELECT {NOTE_FIELDS} FROM notes WHERE telegram_id = %s", (telegram_id,))
            rows = cur.fetchall()
    else:
        rows = conn.execute(f"SELECT {NOTE_FIELDS} FROM notes WHERE telegram_id = ?", (telegram_id,)).fetchall()
    notes = [_note_dict(r) for r in rows]
    notes.sort(key=lambda n: (n["done"], n["due_date"] is None, n["due_date"] or "", n["id"]))
    return notes


def count_notes(conn, telegram_id):
    if PG_DSN:
        with pg_cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM notes WHERE telegram_id = %s", (telegram_id,))
            return cur.fetchone()[0]
    return conn.execute("SELECT COUNT(*) FROM notes WHERE telegram_id = ?", (telegram_id,)).fetchone()[0]


def add_note(conn, telegram_id, subject, text, due_date=None):
    created = now_local().strftime("%Y-%m-%dT%H:%M:%S")
    if PG_DSN:
        with pg_cursor() as cur:
            cur.execute("INSERT INTO notes (telegram_id, subject, text, due_date, done, created_at) "
                        "VALUES (%s, %s, %s, %s, 0, %s) RETURNING id", (telegram_id, subject, text, due_date, created))
            return cur.fetchone()[0]
    cur = conn.execute("INSERT INTO notes (telegram_id, subject, text, due_date, done, created_at) "
                       "VALUES (?, ?, ?, ?, 0, ?)", (telegram_id, subject, text, due_date, created))
    conn.commit()
    return cur.lastrowid


def update_note(conn, telegram_id, note_id, **fields):
    """Меняет поля заметки пользователя (subject, text, due_date, done). Возвращает False, если заметки нет."""
    allowed = {"subject", "text", "due_date", "done"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return True
    names = list(fields)
    if PG_DSN:
        with pg_cursor() as cur:
            cur.execute(f"UPDATE notes SET {', '.join(n + ' = %s' for n in names)} "
                        "WHERE id = %s AND telegram_id = %s", [fields[n] for n in names] + [note_id, telegram_id])
            return cur.rowcount > 0
    cur = conn.execute(f"UPDATE notes SET {', '.join(n + ' = ?' for n in names)} "
                       "WHERE id = ? AND telegram_id = ?", [fields[n] for n in names] + [note_id, telegram_id])
    conn.commit()
    return cur.rowcount > 0


def delete_note(conn, telegram_id, note_id):
    if PG_DSN:
        with pg_cursor() as cur:
            cur.execute("DELETE FROM notes WHERE id = %s AND telegram_id = %s", (note_id, telegram_id))
            return cur.rowcount > 0
    cur = conn.execute("DELETE FROM notes WHERE id = ? AND telegram_id = ?", (note_id, telegram_id))
    conn.commit()
    return cur.rowcount > 0


_LESSON_KIND = re.compile(r"^(?:(?:Л|Пр|Лаб)\.?\s+)+")
# Где кончается название предмета: преподаватель («Иванов И.»), «Прил.», аудитория, «(для …», «вт - …», цифры.
_SUBJECT_TAIL = re.compile(r"(?:\s+|(?<=[а-яё]))(?:[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?\s+[А-ЯЁ]\.|Прил\.|ауд\.|"
                           r"\(для\s|(?:пн|вт|ср|чт|пт|сб)\s+-\s|\(?\d).*$")


def subject_names(lesson: str):
    """Названия предметов из текста пары: без «Л./Пр.», преподавателя и аудитории.
    «Пр. Философия Баранов В.А. 161мбк» -> ["Философия"]; несколько пар через «;» дают несколько названий."""
    names = []
    for part in (lesson or "").strip().split("\n")[0].split(";"):
        name = _SUBJECT_TAIL.sub("", _LESSON_KIND.sub("", part.strip())).strip(" ,.:-")
        if 3 <= len(name) <= 80 and name[0].isupper():
            names.append(name)
    return names


def get_group_subjects(conn, group: GroupRef, limit=60):
    """Названия предметов из расписания группы (для подсказок в заметках), без повторов."""
    rows = conn.execute(
        "SELECT DISTINCT subject FROM lessons WHERE group_name = ? AND program = ? AND course = ?",
        (group.name, group.program, group.course)).fetchall()
    names = {name for (subject,) in rows for name in subject_names(subject)}
    return sorted(names)[:limit]
