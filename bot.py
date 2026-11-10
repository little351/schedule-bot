import asyncio
import csv
import io
import json
import logging
import os
from types import SimpleNamespace
import re
import secrets
from datetime import datetime, timedelta, timezone
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram import (Update, BotCommand, InlineKeyboardMarkup, InlineKeyboardButton, MenuButtonWebApp, WebAppInfo,
                      ReplyKeyboardMarkup)
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, MessageHandler, TypeHandler, ContextTypes, filters
)

import attendance
import lms_sync
import stats
from parser import parse_file
from schedule_parsers import upload_dir
from schedule_parsers.common import load_config
from database import (
    LOCAL_TZ, init_db, set_user_group, get_user_group, save_schedule,
    get_current_week_sheet, get_relative_week_sheet, get_week_sheet_for_date,
    get_week_schedule_grid, now_local, get_groups, get_catalog, find_groups, GroupRef, storage_name,
    touch_user, list_users, user_stats, get_program_name, restore_users, users_without_profile, set_user_profile,
    get_sync_state, set_sync_state, export_backup, import_backup, stats_daily_rows, stats_compact,
    get_day_lessons, TIME_SLOTS, PRACTICE_SLOT
)
from formatting import format_week_text, with_donation_note

TOKEN = os.environ.get("BOT_TOKEN") or ""
WEBAPP_URL = os.environ.get("WEBAPP_URL") or os.environ.get("PUBLIC_URL") or "https://breeding-salvation-splashing.ngrok-free.dev"


def app_url(src):
    """Адрес мини-аппа с меткой, откуда его открыли (для статистики входов: ?src=menu и т.д.)."""
    return f"{WEBAPP_URL}{'&' if '?' in WEBAPP_URL else '?'}src={src}"


# все таблицы (.xlsx и .xls) из этой папки загружаются как расписание; формат файла определяется
# по структуре, направление, курс и группы берутся из заголовков листов (см. schedule_parsers)
SCHEDULE_DIR = "data"

logger = logging.getLogger(__name__)

# Telegram ID владельца: только он может смотреть список пользователей (/users)
OWNER_ID = os.environ.get("OWNER_ID", "").strip()

conn = init_db()
logger.info("Хранилище пользователей: %s", storage_name())
if not os.environ.get("DATABASE_URL"):
    logger.warning("DATABASE_URL не задана: список пользователей хранится в локальной SQLite и пропадёт при деплое")


def load_schedules():
    # порядок источников: свежая подгрузка с сайта вуза, затем загруженное владельцем, затем файлы репозитория
    try:
        records = lms_sync.merged_records(conn, SCHEDULE_DIR)
    except Exception:
        logger.exception("не удалось учесть подгруженные файлы, беру только data/")
        records = lms_sync.disk_records(SCHEDULE_DIR)
    save_schedule(conn, records)
    # при старте только запоминаем, что показываем; при перечитывании (подгрузка, файл от владельца)
    # сравниваем с прошлым и отмечаем группы, у которых поменялись уже известные дни
    global day_marks
    try:
        today = now_local().strftime("%Y-%m-%d")
        marks = lms_sync.day_fingerprints(records, today)
        if day_marks is None:
            lms_sync.load_status(conn)
        else:
            lms_sync.note_changes(conn, lms_sync.changed_days(day_marks, marks, today))
        day_marks = marks
    except Exception:
        logger.exception("не удалось сравнить расписание с прошлым")
    return records


day_marks = None  # отпечатки дней расписания, которое сейчас показывается


def catalog_summary():
    """Что сейчас загружено: по строке на направление, группы по курсам."""
    lines = []
    for program in get_catalog(conn):
        by_course = ", ".join(f"{c['course']} курс {len(c['groups'])}" for c in program["courses"])
        level = " (магистратура)" if program_level(program["code"]) == "m" else ""
        lines.append(f"{program['code']} {program['name']}{level}: групп {by_course}")
    return "\n".join(lines) if lines else "расписание пока не загружено"


NO_GROUP_MSG = "Сначала выбери направление и группу: /group"


START_TEXT = (
    "Привет! Я показываю расписание.\n\n"
    "Как пользоваться:\n"
    "1. Выбери курс, направление и группу кнопками ниже (для магистрантов сначала «Магистратура»). Сменить их можно в любой момент: /group "
    "(или просто напиши номер группы).\n"
    "2. /app — открыть расписание в мини-приложении.\n"
    "3. Расписание прямо в чате: кнопки внизу или команды /week — эта неделя, "
    "/nweek — следующая, /weekdate 05.10 — неделя по дате.\n\n"
    "⚠️ Бот неофициальный: расписание берётся с сайта вуза и может измениться или содержать "
    "неточности, поэтому сверяйся с официальным. Бот работает на бесплатном хостинге: после простоя "
    "он может отвечать с задержкой (до ~30 секунд), а иногда быть временно недоступен. "
    "Если ничего не происходит — подожди и повтори."
)

DONE_TEXT = "Готово, запомнил: {title}.\nВыбери, что показать:"


# --- постоянная клавиатура под полем ввода: те же команды без «/» ---
# Нажатие отправляет в чат текст кнопки, handle_message узнаёт его по KEYBOARD_ACTIONS.
# «Приложение» - обычная текстовая кнопка, а не web_app: мини-апп с кнопки клавиатуры Telegram может открыть
# без подписанных данных пользователя (initData), и API мини-аппа его бы не пустил.
KB_WEEK, KB_NWEEK, KB_DATE, KB_GROUP, KB_APP = "📅 Эта неделя", "➡️ Следующая", "🗓 По дате", "👥 Группа", "📱 Приложение"
KB_STATS, KB_MARK, KB_HEALTH = "📊 Статистика", "✅ Отметиться", "🩺 Состояние"
KEYBOARD_ACTIONS = {KB_WEEK: "week", KB_NWEEK: "nweek", KB_DATE: "date", KB_GROUP: "group", KB_APP: "app"}
OWNER_KEYBOARD_ACTIONS = {KB_STATS: "stats", KB_MARK: "mark", KB_HEALTH: "health"}
DATE_PROMPT = "Напиши дату, например 05.10 или 05.10.2026. Покажу неделю, в которую она попадает."


def main_keyboard(owner=False):
    rows = [[KB_WEEK, KB_NWEEK], [KB_DATE, KB_GROUP, KB_APP]]
    if owner:
        rows.append([KB_STATS, KB_MARK, KB_HEALTH])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True,
                               input_field_placeholder="Номер группы или кнопка ниже")


def keyboard_for(update):
    """Клавиатура для ответа этому пользователю; не в личке (группы, каналы) её не показываем."""
    chat = getattr(update, "effective_chat", None) or getattr(getattr(update, "message", None), "chat", None)
    if chat is None or chat.type != "private":
        return None
    return main_keyboard(owner=_is_owner(update))


def keyboard_action(update, text):
    """Действие по тексту кнопки клавиатуры или None, если это не кнопка (номер группы, дата и т.п.)."""
    if text in KEYBOARD_ACTIONS:
        return KEYBOARD_ACTIONS[text]
    if text in OWNER_KEYBOARD_ACTIONS and _is_owner(update):
        return OWNER_KEYBOARD_ACTIONS[text]
    return None


def actions_markup():
    """Кнопки под выбранной группой: то же, что команды /app, /week, /nweek, /weekdate, /group."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📱 Мини-приложение", web_app=WebAppInfo(url=app_url("pick")))],
        [InlineKeyboardButton("Неделя", callback_data="a:week"), InlineKeyboardButton("След. неделя", callback_data="a:nweek")],
        [InlineKeyboardButton("По дате", callback_data="a:date"), InlineKeyboardButton("Сменить группу", callback_data="a:group")],
    ])


# --- выбор уровня, курса, направления и группы кнопками ---
# callback_data: l:<уровень> | c:<курс>[:<уровень>] | p:<курс>:<направление> | g:<направление>:<курс>:<группа>
#                b:l (назад к уровням) | b:c[:<уровень>] (назад к курсам) | b:<курс>[:<уровень>] (назад к направлениям)
# уровень: b - бакалавриат, m - магистратура; без уровня - бакалавриат (так работают кнопки старых сообщений)
LEVEL_NAMES = {"b": "Бакалавриат", "m": "Магистратура"}


program_level = lms_sync.program_level


def _levels(catalog):
    return sorted({program_level(p["code"]) for p in catalog})


def _courses(catalog):
    return sorted({c["course"] for p in catalog for c in p["courses"]})


def _programs_of_course(catalog, course):
    return [p for p in catalog if any(c["course"] == course for c in p["courses"])]


def _group_button(group, with_program=False):
    label = group.title if with_program else group.name
    return InlineKeyboardButton(label, callback_data=f"g:{group.program}:{group.course}:{group.name}")


def _group_buttons(groups, per_row=3, with_program=False):
    buttons = [_group_button(g, with_program) for g in groups]
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def _back_row(target):
    return [[InlineKeyboardButton("‹ Назад", callback_data=f"b:{target}")]] if target else []


def _suffix(level):
    return ":m" if level == "m" else ""


def picker_screen(step="course", course=None, program=None, level=None):
    """Текст и клавиатура очередного шага: уровень -> курс -> направление -> группа.
    Шаг уровня есть, только если в расписании есть и бакалавриат, и магистратура;
    шаг направления пропускается, если на курсе одно направление."""
    catalog = get_catalog(conn)
    if not catalog:
        return "Расписание пока не загружено, попробуй позже.", None
    levels = _levels(catalog)
    if step == "level" and len(levels) > 1:
        buttons = [InlineKeyboardButton(LEVEL_NAMES[lv], callback_data=f"l:{lv}") for lv in levels]
        return "Выбери уровень обучения:", InlineKeyboardMarkup([buttons])
    if level not in levels:
        level = "b" if "b" in levels else levels[0]
    catalog = [p for p in catalog if program_level(p["code"]) == level]
    courses = _courses(catalog)
    head = f"{LEVEL_NAMES[level]}, " if len(levels) > 1 else ""

    if step in ("course", "level") or course not in courses:
        buttons = [InlineKeyboardButton(f"{c} курс", callback_data=f"c:{c}{_suffix(level)}") for c in courses]
        rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
        text = f"{LEVEL_NAMES[level]}. Выбери курс:" if head else "Выбери курс:"
        return text, InlineKeyboardMarkup(rows + (_back_row("l") if head else []))

    programs = _programs_of_course(catalog, course)
    if step == "program" and len(programs) > 1:
        rows = [[InlineKeyboardButton(f"{p['code']} {p['name']}", callback_data=f"p:{course}:{p['code']}")]
                for p in programs]
        return f"{head}{course} курс. Выбери направление подготовки:", \
            InlineKeyboardMarkup(rows + _back_row("c" + _suffix(level)))

    entry = next((p for p in programs if p["code"] == program), programs[0])
    groups = get_groups(conn, entry["code"], course)
    back = (str(course) if len(programs) > 1 else "c") + _suffix(level)
    return (f"{head}{course} курс, {entry['code']} {entry['name']}. Выбери группу:",
            InlineKeyboardMarkup(_group_buttons(groups) + _back_row(back)))


async def group_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats.count("pick.start")
    text, markup = picker_screen("level")
    await update.message.reply_text(text, reply_markup=markup)


async def answer_query(query):
    """Ответ на нажатие кнопки. Если сервер проспал дольше, чем Telegram ждёт ответа (Render будит его до ~30 с),
    Telegram отвечает «query is too old»: нажатие всё равно выполняем, просто без снятия «часиков» на кнопке."""
    try:
        await query.answer()
    except BadRequest as e:
        if not _is_stale_query(e):
            raise
        logger.info("нажатие кнопки обработано с опозданием: %s", e)


async def on_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await answer_query(query)
    parts = (query.data or "").split(":", 3)

    if parts[0] == "g" and len(parts) == 4 and parts[2].isdigit():
        group = GroupRef(parts[1], int(parts[2]), parts[3])
        if group not in get_groups(conn):
            await query.edit_message_text("Такой группы нет в расписании. Попробуй /group ещё раз.")
            return
        set_user_group(conn, query.from_user.id, group)
        stats.count("pick.done")
        await query.edit_message_text(DONE_TEXT.format(title=group.title), reply_markup=actions_markup())
        return

    level = parts[2] if len(parts) >= 3 and parts[0] in ("c", "b") else None
    if parts[0] == "l" and len(parts) >= 2:
        text, markup = picker_screen("course", level=parts[1])
    elif parts[0] == "c" and len(parts) >= 2 and parts[1].isdigit():
        text, markup = picker_screen("program", course=int(parts[1]), level=level)
    elif parts[0] == "p" and len(parts) >= 3 and parts[1].isdigit():
        text, markup = picker_screen("groups", course=int(parts[1]), program=parts[2], level=program_level(parts[2]))
    elif parts[0] == "b" and len(parts) >= 2 and parts[1].isdigit():
        text, markup = picker_screen("program", course=int(parts[1]), level=level)
    elif parts[0] == "b" and len(parts) >= 2 and parts[1] == "c":
        text, markup = picker_screen("course", level=level)
    else:
        text, markup = picker_screen("level")
    await query.edit_message_text(text, reply_markup=markup)


async def on_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопки под выбранной группой вызывают те же обработчики, что и команды."""
    query = update.callback_query
    await answer_query(query)
    action = (query.data or "").split(":", 1)[-1]
    # обработчики команд читают update.message и update.effective_user: подставляем сообщение с кнопкой и нажавшего
    shim = SimpleNamespace(message=query.message, effective_user=query.from_user,
                           effective_chat=getattr(query.message, "chat", None), callback_query=query)
    if action == "week":
        await week(shim, context)
    elif action == "nweek":
        await nweek(shim, context)
    elif action == "group":
        await group_command(shim, context)
    elif action == "date":
        context.user_data["await_date"] = True
        await query.message.reply_text(DATE_PROMPT)


STATS_COMMANDS = {"start", "week", "nweek", "weekdate", "app", "group"}
STATS_BUTTONS = {"a:week": "btn.week", "a:nweek": "btn.nweek", "a:date": "btn.date", "a:group": "btn.group"}
STATS_KEYBOARD = {"week", "nweek", "date", "group", "app"}


def count_action(update):
    """Счётчики статистики по обращению к боту: команда, кнопка или текст. Только память, без базы.
    Блокировка бота и прочие служебные обновления действием не считаются."""
    message, query = update.message, update.callback_query
    if message is None and query is None:
        return
    stats.seen(update.effective_user.id, stats.SEEN_BOT)
    if query is not None:
        if query.data in STATS_BUTTONS:
            stats.count(STATS_BUTTONS[query.data])
        return
    text = (message.text or "").strip()
    if text.startswith("/"):
        command = text[1:].split(maxsplit=1)[0].split("@", 1)[0].lower() if len(text) > 1 else ""
        if command in STATS_COMMANDS:
            stats.count("cmd." + command)
    elif text in KEYBOARD_ACTIONS and KEYBOARD_ACTIONS[text] in STATS_KEYBOARD:
        stats.count("kb." + KEYBOARD_ACTIONS[text])
    elif text:
        stats.count("msg.text")
    elif message.document:
        stats.count("msg.document")


async def track_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Срабатывает на каждое обращение, ничего не отвечает и не мешает остальным обработчикам."""
    user = update.effective_user
    if not user or user.is_bot:
        return
    try:
        count_action(update)
    except Exception:
        logger.exception("статистика: не удалось посчитать обращение")
    try:
        touch_user(conn, user.id, user.username, user.full_name)
    except Exception:
        # учёт не должен ломать работу бота, но ошибку нужно видеть в логах
        logger.exception("не удалось записать пользователя %s", user.id)


def _moscow_date(value):
    """'2026-10-09T22:30:00+00:00' (UTC) -> '10.10.2026' по Москве; дата без времени - как есть; пусто - None."""
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return value[:10]
    if len(value) > 10:  # время есть: переводим из UTC в Москву
        moment = (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).astimezone(LOCAL_TZ)
    return moment.strftime("%d.%m.%Y")


def _seen_range(first_seen, last_seen):
    """Даты первого и последнего обращения по Москве: '01.09.2026–09.10.2026', одна дата или '?'."""
    first, last = _moscow_date(first_seen), _moscow_date(last_seen)
    if first and last and first != last:
        return f"{first}–{last}"
    return last or first or "?"


def _parse_seen(text):
    """Обратно к (first_seen, last_seen) в ISO: '01.09.2026–09.10.2026', '09.10.2026', '2026-10-09' (старый вид) или '?'."""
    def iso(part):
        try:
            return datetime.strptime(part, "%d.%m.%Y").strftime("%Y-%m-%d")
        except ValueError:
            return part
    if text == "?":
        return None, None
    first, _, last = text.partition("–")
    return (iso(first), iso(last)) if last else (iso(first), iso(first))


def format_users_messages(rows):
    """Список пользователей, разбитый на сообщения короче лимита Telegram (4096)."""
    header = f"Пользователей: {len(rows)}"
    lines = []
    for telegram_id, username, first_name, group_name, first_seen, last_seen, program, course in rows:
        handle = f"@{username}" if username else "(без username)"
        last = _seen_range(first_seen, last_seen)
        if group_name and program:
            group = f"{group_name} ({program}, {course or 1} курс)"
        else:
            group = group_name or "группа не выбрана"
        lines.append(f"{handle} · {first_name or '—'} · {group} · {last} · id {telegram_id}")

    messages, current = [], header
    for line in lines:
        if len(current) + len(line) + 1 > 3500:
            messages.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line
    messages.append(current)
    return messages


async def backfill_profiles(bot):
    """Старые записи (созданные до учёта пользователей) не знают ни имени, ни username.
    Telegram отдаёт их по ID тем, кто когда-либо писал боту, поэтому дозаполняем пачками."""
    for telegram_id in users_without_profile(conn, limit=50):
        try:
            chat = await bot.get_chat(telegram_id)
        except Exception:
            continue  # пользователь заблокировал бота или удалил аккаунт
        set_user_profile(conn, telegram_id, chat.username, chat.full_name or chat.first_name)


def format_users_csv(rows):
    """Список пользователей в CSV (для резервной копии, открывается в Excel)."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["telegram_id", "username", "first_name", "group_name", "first_seen", "last_seen", "program", "course"])
    writer.writerows(rows)
    return out.getvalue().encode("utf-8-sig")


# строка списка /users: «@ivan · Иван · 4 группа (49.03.04, 2 курс) · 01.09.2026–09.10.2026 · id 123»
# (первое и последнее обращение по Москве); в старых форматах курса нет: «4 группа (49.03.04)», такие
# пользователи получают 1 курс, а дата одна: «2026-10-09»
USER_LINE_RE = re.compile(r"^(?:@(\S+)|\(без username\)) · (.*?) · (.*?) · (\S+) · id (-?\d+)\s*$", re.MULTILINE)
GROUP_PROGRAM_RE = re.compile(r"^(.*?)\s*\((\d{2}\.\d{2}\.\d{2})(?:, (\d) курс)?\)$")


def parse_users_backup(text):
    """Строки для restore_users из текста: сообщения /users (можно переслать боту) или файл /backup (CSV)."""
    rows = []
    for username, first_name, group, last, telegram_id in USER_LINE_RE.findall(text):
        program = course = None
        m = GROUP_PROGRAM_RE.match(group)
        if m:
            group, program, course = m.group(1), m.group(2), int(m.group(3) or 1)
        group = None if group.startswith("группа не выбрана") else group
        first_seen, last_seen = _parse_seen(last)
        first_name = None if first_name == "—" else first_name
        rows.append((int(telegram_id), username or None, first_name, group, first_seen, last_seen, program, course))
    if rows:
        return rows
    for record in csv.DictReader(io.StringIO(text.lstrip("\ufeff"))):
        if (record.get("telegram_id") or "").lstrip("-").isdigit():
            program = record.get("program") or None
            course = int(record["course"]) if (record.get("course") or "").isdigit() else (1 if program else None)
            rows.append((
                int(record["telegram_id"]), record.get("username") or None, record.get("first_name") or None,
                record.get("group_name") or None, record.get("first_seen") or None,
                record.get("last_seen") or None, program, course,
            ))
    return rows


def _is_owner(update):
    return OWNER_ID.isdigit() and update.effective_user.id == int(OWNER_ID)


BACKUP_KIND = "schedule-bot-backup"
BACKUP_INTERVAL_DAYS = 7  # как часто бот сам присылает владельцу резервную копию
BACKUP_TITLES = {"users": "пользователей", "notes": "заметок", "game_scores": "рекордов игры",
                 "tetris_scores": "рекордов игры «Блоки»",
                 "tetris_hard_scores": "рекордов «Блоков» (сложный)",
                 "blocks_wallet": "кошельков игры «Блоки»",
                 "sync_state": "настроек подгрузки"}


def make_backup(conn):
    """(имя файла, байты, подпись): все пользователи, заметки, рекорды игры и состояние подгрузки в JSON."""
    data = {"kind": BACKUP_KIND, "version": 1, "created_at": now_local().strftime("%Y-%m-%dT%H:%M:%S"),
            **export_backup(conn)}
    counts = ", ".join(f"{BACKUP_TITLES[t]} {len(rows)}" for t, rows in data["tables"].items())
    caption = (f"Резервная копия: {counts}.\n"
               "Чтобы восстановить, пришли этот файл боту. Подгруженное с сайта вуза вернёт /sync.")
    name = f"backup-{now_local().strftime('%Y-%m-%d')}.json"
    return name, json.dumps(data, ensure_ascii=False).encode("utf-8"), caption


def parse_backup(content):
    """Данные полной копии или None, если это не она."""
    try:
        data = json.loads(content)
    except ValueError:
        return None
    return data if isinstance(data, dict) and data.get("kind") == BACKUP_KIND else None


async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name, data, caption = make_backup(conn)
    await update.message.reply_document(document=io.BytesIO(data), filename=name, caption=caption)


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await backfill_profiles(context.bot)
    rows = list_users(conn)
    await update.message.reply_text(f"Хранилище пользователей: {storage_name()}")
    for text in format_users_messages(rows):
        await update.message.reply_text(text)


def format_stats(stats, program_name=lambda code: code):
    """Текст /stats: только счётчики, без личных данных."""
    lines = [f"Всего пользователей: {stats['total']}", f"Без выбранной группы: {stats['no_group']}", ""]
    lines.append("Новые (первое обращение): "
                 f"за сутки {stats['new'][1]}, за 7 дней {stats['new'][7]}, за 30 дней {stats['new'][30]}")
    lines.append("Активные (последнее обращение): "
                 f"за сутки {stats['active'][1]}, за 7 дней {stats['active'][7]}, за 30 дней {stats['active'][30]}")
    if stats["no_first_seen"] or stats["no_last_seen"]:
        lines.append(f"Без даты обращения: {max(stats['no_first_seen'], stats['no_last_seen'])} "
                     "(записи из старой копии, в «новых» и «активных» не учтены)")
    if stats["courses"]:
        by_level = stats.get("level_courses") or {("b", c): n for c, n in stats["courses"].items()}
        levels = sorted({level for level, _ in by_level})
        lines += ["", "По курсам:"]
        for level in levels:
            if len(levels) > 1:
                lines.append(f"{LEVEL_NAMES[level]}:")
            lines += [f"{c} курс: {n}" for (lv, c), n in sorted(by_level.items(), key=lambda kv: (kv[0][1] is None, kv[0][1]))
                      if lv == level]
        lines += ["", "По направлениям:"]
        lines += [f"{program_name(p)}{', магистратура' if program_level(p) == 'm' else ''} ({p}): {n}"
                  for p, n in sorted(stats["programs"].items(), key=lambda kv: (-kv[1], kv[0] or ""))]
        lines += ["", "Популярные группы:"]
        lines += [f"{name} ({p}, {c} курс): {n}" for (p, c, name), n in stats["top_groups"]]
    return "\n".join(lines)


STATS_MAX_DAYS = 180


async def stats_flush_now():
    """Записать накопленные счётчики в базу (из потока: запрос к базе не должен держать бота)."""
    try:
        await asyncio.to_thread(stats.flush, conn)
    except Exception:
        logger.exception("статистика: не удалось записать счётчики, попробую в следующий раз")


async def stats_flusher():
    """Раз в 10 минут счётчики уходят в базу одним запросом на таблицу (пустые - вовсе без запроса)."""
    while True:
        await asyncio.sleep(stats.FLUSH_EVERY)
        await stats_flush_now()


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/stats - сводка действий за 7 дней и пользователи по курсам; /stats 30 - за 30 дней;
    /stats csv - файл по дням для Excel (за 30 дней или за указанное число дней)."""
    args = [a.lower() for a in (context.args or [])]
    as_csv = "csv" in args
    days = next((int(a) for a in args if a.isdigit()), 30 if as_csv else 7)
    days = max(1, min(days, STATS_MAX_DAYS))
    await stats_flush_now()  # чтобы в отчёт попало и то, что ещё не записано
    if as_csv:
        start = (now_local().date() - timedelta(days=days - 1)).isoformat()
        rows = await asyncio.to_thread(database_stats_rows, start)
        data = await asyncio.to_thread(stats.report_data, conn, days)
        await update.message.reply_document(document=io.BytesIO(stats.format_csv(rows, data["by_day"]).encode("utf-8")),
                                            filename=f"stats-{start}-{now_local():%Y-%m-%d}.csv",
                                            caption=f"Статистика по дням за {days} дн.")
        return
    data = await asyncio.to_thread(stats.report_data, conn, days)
    for part in _chunks(stats.format_report(data)):
        await update.message.reply_text(part)
    users = user_stats(list_users(conn))
    await update.message.reply_text(f"Хранилище пользователей: {storage_name()}\n\n"
                                    + format_stats(users, lambda code: get_program_name(conn, code)))


def database_stats_rows(start):
    return stats_daily_rows(conn, start)


async def lms_check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Диагностика доступа к сайту вуза: на каком шаге обрывается вход. Пароль в ответ не попадает."""
    import lms_check
    do_login = bool(context.args) and context.args[0].lower() == "login"
    await update.message.reply_text("Проверяю доступ к сайту вуза, это займёт до минуты...")

    async def run():
        # в фоне, как /sync: вебхук должен ответить Telegram сразу, иначе он повторит команду
        try:
            lines = await asyncio.to_thread(
                lms_check.run_checks, login=os.environ.get("LMS_LOGIN", "").strip(),
                password=os.environ.get("LMS_PASSWORD", ""), do_login=do_login)
            await update.message.reply_text("\n".join(lines))
        except Exception:
            logger.exception("/lms_check: ошибка")

    context.application.create_task(run())


# --- отметка посещаемости на сайте вуза (только владелец): /mark ---
# Страницы посещаемости владельца (mod/attendance/view.php?id=…) хранятся в sync_state, читаются только по /mark.
MARK_PAGES_KEY = "attendance_pages"
MARK_CONFIRM_MINUTES = 15  # сколько действует вопрос «Отметиться?»
MARK_WINDOW_BEFORE = 15  # за сколько минут до начала пара считается текущей


def mark_pages():
    try:
        pages = json.loads(get_sync_state(conn, MARK_PAGES_KEY) or "[]")
    except ValueError:
        return []
    return [p for p in pages if isinstance(p, str)]


def describe_lesson(lessons, now):
    """Текст о паре, идущей в момент now (за MARK_WINDOW_BEFORE минут до начала уже считается), или о ближайшей."""
    minutes = now.hour * 60 + now.minute
    upcoming = practice = None
    for lesson in lessons:
        if lesson["time_start"] == PRACTICE_SLOT:
            practice = lesson["subject"]
            continue
        (h1, m1), (h2, m2) = (map(int, lesson[k].split(":")) for k in ("time_start", "time_end"))
        slot = f"{lesson['time_start']}-{lesson['time_end']}".replace(":", ".")
        number = f"{TIME_SLOTS.index(slot) + 1} пара, " if slot in TIME_SLOTS else ""
        title = f"{number}{lesson['time_start']}–{lesson['time_end']}: «{lesson['subject']}»"
        if h1 * 60 + m1 - MARK_WINDOW_BEFORE <= minutes <= h2 * 60 + m2:
            return f"Сейчас {title}."
        if upcoming is None and minutes < h1 * 60 + m1:
            upcoming = title
    if practice:
        return f"Сегодня практика: «{practice}»."
    return "Сейчас по расписанию пары нет" + (f", следующая: {upcoming}." if upcoming else ".")


def current_lesson(now=None):
    """Текст о текущей паре владельца по расписанию его группы в боте."""
    now = now or now_local()
    group = get_user_group(conn, int(OWNER_ID)) if OWNER_ID.isdigit() else None
    if not group:
        return "Группа у вас в боте не выбрана, текущую пару не знаю."
    # недели в базе хранятся как datetime без часового пояса, поэтому и день передаём так же (как мини-апп)
    _, lessons = get_day_lessons(conn, group, datetime(now.year, now.month, now.day))
    return describe_lesson(lessons, now)


def _lms_client():
    login, password = lms_sync.credentials()
    base = (lms_sync.load_sources(SCHEDULE_DIR).get("base_url") or os.environ.get("LMS_BASE_URL")
            or "https://lms.sportedu.ru")
    client = lms_sync.MoodleClient(base, login, password)
    try:
        client.login()
    except Exception:
        client.close()
        raise
    return client


def _find_open_marks(pages):
    client = _lms_client()
    try:
        return attendance.find_open(client, pages)
    finally:
        client.close()


def _do_mark(page, sessid, password=None):
    client = _lms_client()
    try:
        return attendance.mark(client, page, sessid, password)
    finally:
        client.close()


async def mark_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/mark - отметиться на текущей паре; /mark add <ссылка>, /mark del <номер>, /mark list - страницы отметки."""
    args = context.args or []
    pages = mark_pages()
    if args and args[0].lower() in ("add", "del", "list"):
        action = args[0].lower()
        if action == "add":
            added = [p for p in (attendance.page_path(a) for a in args[1:]) if p and p not in pages]
            if not added:
                await update.message.reply_text("Пришлите ссылку вида https://lms.sportedu.ru/mod/attendance/view.php?id=505662 "
                                                "после /mark add (можно несколько через пробел).")
                return
            pages += added
            set_sync_state(conn, MARK_PAGES_KEY, json.dumps(pages))
        elif action == "del":
            numbers = {int(a) for a in args[1:] if a.isdigit()}
            pages = [p for i, p in enumerate(pages, 1) if i not in numbers]
            set_sync_state(conn, MARK_PAGES_KEY, json.dumps(pages))
        lines = [f"{i}. {lms_sync.load_sources(SCHEDULE_DIR).get('base_url', 'https://lms.sportedu.ru')}{p}"
                 for i, p in enumerate(pages, 1)]
        await update.message.reply_text("Страницы отметки:\n" + "\n".join(lines) if lines else
                                        "Страниц отметки пока нет. Добавьте: /mark add <ссылка>")
        return
    if not pages:
        await update.message.reply_text("Сначала добавьте страницы отметки по предметам: /mark add <ссылка> "
                                        "(ссылки вида …/mod/attendance/view.php?id=…)")
        return
    login, password = lms_sync.credentials()
    if not login or not password:
        await update.message.reply_text("Нет LMS_LOGIN или LMS_PASSWORD в Render, на сайт вуза войти нельзя.")
        return
    lesson_text = current_lesson()
    await update.message.reply_text(f"{lesson_text}\nИщу открытую отметку на {len(pages)} стр. сайта вуза...")

    async def run():
        # в фоне, как /sync: вебхук должен ответить Telegram сразу
        try:
            found, problems = await asyncio.to_thread(_find_open_marks, pages)
        except lms_sync.SyncError as e:
            await update.message.reply_text(f"Не удалось зайти на сайт вуза: {lms_sync.scrub(e)}")
            return
        except Exception as e:
            logger.exception("/mark: ошибка")
            await update.message.reply_text(f"Ошибка при поиске отметки: {lms_sync.scrub(type(e).__name__)}")
            return
        extra = ("\n\nНе открылись: " + "; ".join(lms_sync.scrub(p) for p in problems)) if problems else ""
        if not found:
            await update.message.reply_text("Открытой отметки сейчас нет ни на одной странице. Обычно её открывает "
                                            "преподаватель во время пары." + extra)
            return
        pending = context.bot_data.setdefault("mark_pending", {})
        for item in found:
            token = secrets.token_hex(4)
            pending[token] = dict(item, at=datetime.now(timezone.utc))
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Да", callback_data=f"mk:y:{token}"),
                                            InlineKeyboardButton("❌ Нет", callback_data=f"mk:n:{token}")]])
            await update.message.reply_text(f"Открыта отметка: «{item['title'] or item['page']}».\n"
                                            f"Отметить вас как присутствующего?" + extra, reply_markup=markup)
            extra = ""

    context.application.create_task(run())


def _mark_take(context, token):
    item = context.bot_data.get("mark_pending", {}).pop(token, None)
    if item and datetime.now(timezone.utc) - item["at"] > timedelta(minutes=MARK_CONFIRM_MINUTES):
        return None
    return item


async def _mark_run(bot, chat_id, context, item, password=None):
    try:
        result, detail = await asyncio.to_thread(_do_mark, item["page"], item["sessid"], password)
    except lms_sync.SyncError as e:
        result, detail = "error", str(e)
    except Exception as e:
        logger.exception("/mark: ошибка отметки")
        result, detail = "error", type(e).__name__
    title = item["title"] or item["page"]
    if result == "ok":
        text = f"✅ Отмечено: «{title}», статус «{detail}»."
    elif result == "password":
        token = secrets.token_hex(4)
        context.bot_data.setdefault("mark_pending", {})[token] = dict(item, at=datetime.now(timezone.utc))
        context.bot_data["mark_password"] = token
        text = f"Для отметки на «{title}» нужен пароль от преподавателя. Пришлите его следующим сообщением."
    else:
        text = f"❌ Не получилось отметиться на «{title}»: {lms_sync.scrub(detail)}"
    await bot.send_message(chat_id=chat_id, text=text)


async def mark_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await answer_query(query)
    if not _is_owner(update):
        return
    _, answer, token = (query.data.split(":") + ["", ""])[:3]
    item = _mark_take(context, token)
    if not item:
        await query.edit_message_text("Вопрос устарел. Запустите /mark ещё раз.")
        return
    if answer != "y":
        await query.edit_message_text(f"Не отмечаюсь на «{item['title'] or item['page']}».")
        return
    await query.edit_message_text(f"Отмечаюсь на «{item['title'] or item['page']}»...")
    context.application.create_task(_mark_run(context.bot, query.message.chat.id, context, item))


async def mark_password_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сообщение владельца после вопроса о пароле: это пароль от преподавателя. True - сообщение обработано."""
    token = context.bot_data.pop("mark_password", None)
    item = _mark_take(context, token) if token else None
    if not item:
        return False
    await update.message.reply_text("Отправляю отметку с паролем...")
    context.application.create_task(_mark_run(context.bot, update.message.chat.id, context, item,
                                              update.message.text.strip()))
    return True


LMS_FAIL_REPEAT_DAYS = 3  # об одной и той же неполадке подгрузки напоминаем не чаще


def _chunks(text, size=3800):
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


async def run_lms_sync(list_only=False):
    """Один запуск подгрузки; после успешной смены файлов расписание перечитывается. Возвращает Report."""
    report = await lms_sync.run_sync(conn, SCHEDULE_DIR, list_only=list_only)
    if list_only:
        return report
    if report.changed:
        try:
            await asyncio.to_thread(load_schedules)
        except Exception:
            logger.exception("после подгрузки не удалось перечитать расписание")
            report.problem("файлы сохранены, но расписание не перечиталось, перезапустите сервис")
    if lms_sync.missing_settings(lms_sync.load_sources(SCHEDULE_DIR)) == []:
        lms_sync.record_run(conn, report, SCHEDULE_DIR)
    return report


sync_running = False


async def sync_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/sync - запустить подгрузку расписания вручную; /sync list - какие файлы видны на страницах курса."""
    list_only = bool(context.args) and context.args[0].lower() == "list"
    if sync_running:
        await update.message.reply_text("Подгрузка уже идёт, дождись отчёта.")
        return
    await update.message.reply_text("Смотрю страницы курса..." if list_only else "Запускаю подгрузку расписания...")

    async def run():
        # в фоне: вебхук должен ответить Telegram сразу, иначе он повторяет команду по кругу
        global sync_running
        sync_running = True
        try:
            report = await run_lms_sync(list_only=list_only)
            text = report.text() or "Нечего сообщить."
            if not list_only:
                text += "\n\n" + lms_sync.source_summary(conn)
        except Exception as e:
            logger.exception("/sync: ошибка")
            text = lms_sync.scrub(f"Подгрузка упала с ошибкой {type(e).__name__}. Бот работает на запасных источниках.")
        finally:
            sync_running = False
        try:
            for part in _chunks(text):
                await update.message.reply_text(part)
        except Exception:
            logger.exception("/sync: не удалось отправить отчёт")

    context.application.create_task(run())


# --- Автозадачи: резервная копия и подгрузка расписания ---
# Запускаются из одного места двумя путями: таймером, пока сервер не спит, и раз в сутки вызовом /cron
# из GitHub Actions (.github/workflows/daily-jobs.yml), который будит бесплатный Render. Копия идёт первой:
# если подгрузка уронит процесс, копия к этому времени уже отправлена.
PROCESS_STARTED = datetime.now(timezone.utc)
JOBS_EVERY = 1800  # таймер проверяет, не пора ли, раз в полчаса
STALE_SYNC_HOURS = 30  # подгрузки нет дольше: владелец получает предупреждение
STALE_BACKUP_DAYS = BACKUP_INTERVAL_DAYS + 1
INTERRUPTED_RETRY_HOURS = 6  # оборванную подгрузку повторяем не раньше, чтобы не попасть в круг
ALERT_REPEAT_HOURS = 24  # одно и то же предупреждение владельцу не чаще
_jobs_lock = asyncio.Lock()
job_status = {"checked_at": None, "source": None, "error": None}  # для /health, живёт до перезапуска


def _utc(value):
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


async def _alert_owner(bot, key, text):
    """Предупреждение владельцу; одно и то же (по key) не чаще раза в ALERT_REPEAT_HOURS часов,
    время хранится в базе, потому что сервер часто перезапускается."""
    if not OWNER_ID.isdigit():
        return
    now = datetime.now(timezone.utc)
    last = _utc(get_sync_state(conn, "alert_" + key))
    if last and now - last < timedelta(hours=ALERT_REPEAT_HOURS):
        return
    set_sync_state(conn, "alert_" + key, now.isoformat(timespec="seconds"))
    for part in _chunks(lms_sync.scrub(text)):
        await bot.send_message(chat_id=int(OWNER_ID), text=part)


async def backup_if_due(bot):
    """Раз в BACKUP_INTERVAL_DAYS дней присылает владельцу резервную копию: она лежит в Telegram,
    вне базы и вне Render, и переживёт и потерю базы, и её остановку."""
    if not OWNER_ID.isdigit():
        return
    last = _utc(get_sync_state(conn, "backup_sent_at"))
    if last and datetime.now(timezone.utc) - last < timedelta(days=BACKUP_INTERVAL_DAYS):
        return
    name, data, caption = make_backup(conn)
    await bot.send_document(chat_id=int(OWNER_ID), document=io.BytesIO(data), filename=name,
                            caption="Еженедельная " + caption[0].lower() + caption[1:])
    set_sync_state(conn, "backup_sent_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))


def sync_ready():
    """Плановая подгрузка возможна: включена, логин и пароль заданы, и владелец уже запускал /sync
    (плановые запуски начинаются после первого ручного, так видно, что всё работает)."""
    return (lms_sync.sync_enabled() and not lms_sync.missing_settings(lms_sync.load_sources(SCHEDULE_DIR))
            and bool(get_sync_state(conn, "last_run")))


async def lms_if_due(bot):
    """Плановая подгрузка. Перед запуском ставится отметка «начата»: если процесс погибнет посередине
    (память, засыпание, выкладка), следующий запуск это увидит, сообщит владельцу и повторит не сразу.
    О проблемах подгрузки сообщает владельцу, об одной и той же не чаще раза в LMS_FAIL_REPEAT_DAYS."""
    if not sync_ready() or not lms_sync.is_due(conn, schedule_dir=SCHEDULE_DIR):
        return
    now = datetime.now(timezone.utc)
    started, last_run = _utc(get_sync_state(conn, "lms_started_at")), _utc(get_sync_state(conn, "last_run"))
    if started and (not last_run or started > last_run):
        if now - started < timedelta(hours=INTERRUPTED_RETRY_HOURS):
            return
        await _alert_owner(bot, "sync_interrupted",
                           f"Плановая подгрузка расписания, начатая {lms_sync._moscow_time(started.isoformat())}, "
                           "не дошла до конца: скорее всего, сервер перезапустился посередине или ему не хватило памяти. "
                           "Запускаю ещё раз. Подробности в Render → Events и Logs.")
    set_sync_state(conn, "lms_started_at", now.isoformat(timespec="seconds"))
    report = await run_lms_sync()
    if report.problems and OWNER_ID.isdigit():
        signature = report.signature()
        last = get_sync_state(conn, "notified_signature")
        last_at = get_sync_state(conn, "notified_at")
        stale = not last_at or (lms_sync._now() - datetime.fromisoformat(last_at)).days >= LMS_FAIL_REPEAT_DAYS
        if signature != last or stale:
            set_sync_state(conn, "notified_signature", signature)
            set_sync_state(conn, "notified_at", lms_sync._now().isoformat(timespec="seconds"))
            for part in _chunks("Подгрузка расписания с сайта: есть проблемы.\n\n" + report.text()):
                await bot.send_message(chat_id=int(OWNER_ID), text=part)
    elif not report.problems:
        set_sync_state(conn, "notified_signature", "")


async def warn_if_stale(bot):
    """Если автозадачи давно не срабатывали, владелец узнаёт об этом, а не замечает сам через неделю."""
    now = datetime.now(timezone.utc)
    last_run = _utc(get_sync_state(conn, "last_run"))
    if last_run and sync_ready() and now - last_run > timedelta(hours=STALE_SYNC_HOURS):
        await _alert_owner(bot, "sync_stale", f"Подгрузки расписания не было с {lms_sync._moscow_time(last_run.isoformat())}. "
                                              "Что происходит, видно в /health.")
    last_backup = _utc(get_sync_state(conn, "backup_sent_at"))
    if OWNER_ID.isdigit() and last_backup and now - last_backup > timedelta(days=STALE_BACKUP_DAYS):
        await _alert_owner(bot, "backup_stale", f"Автоматической резервной копии не было с "
                                                f"{lms_sync._moscow_time(last_backup.isoformat())}. Что происходит, видно в /health.")


_weekly_done_for = None  # понедельник, за который сводка уже отправлена (чтобы не читать базу каждые полчаса)


async def weekly_stats_if_due(bot):
    """Раз в неделю, начиная с утра понедельника, владелец получает сводку за прошлую неделю (пн-вс).
    Заодно строки «кто был в этот день» старше полугода сворачиваются в дневные итоги."""
    global _weekly_done_for
    if not OWNER_ID.isdigit():
        return
    now = now_local()
    monday = now.date() - timedelta(days=now.weekday())
    if _weekly_done_for == monday.isoformat():
        return
    if (get_sync_state(conn, "stats_weekly_sent") or "") >= monday.isoformat():
        _weekly_done_for = monday.isoformat()
        return
    await stats_flush_now()
    sunday = datetime.combine(monday - timedelta(days=1), datetime.min.time())
    data = await asyncio.to_thread(stats.report_data, conn, 7, sunday)
    if data["by_day"] or data["totals"]:
        text = stats.format_report(data, title=f"📊 Сводка за неделю {data['start']:%d.%m}–{data['today']:%d.%m}")
        for part in _chunks(text):
            await bot.send_message(chat_id=int(OWNER_ID), text=part)
    await asyncio.to_thread(stats_compact, conn, (now.date() - timedelta(days=stats.KEEP_SEEN_DAYS)).isoformat())
    set_sync_state(conn, "stats_weekly_sent", monday.isoformat())
    _weekly_done_for = monday.isoformat()


async def run_due_jobs(bot, source):
    """Выполнить всё, чему пора: копия, подгрузка, проверка давности. Одновременно идёт только один прогон."""
    if _jobs_lock.locked():
        return False
    async with _jobs_lock:
        job_status.update(checked_at=datetime.now(timezone.utc), source=source)
        for name, job in (("резервная копия", backup_if_due), ("подгрузка расписания", lms_if_due),
                          ("проверка автозадач", warn_if_stale), ("сводка статистики", weekly_stats_if_due)):
            try:
                await job(bot)
            except Exception as e:
                logger.exception("автозадача «%s»: ошибка", name)
                job_status["error"] = f"{name}: {type(e).__name__}: {e}"[:300]
                try:
                    await _alert_owner(bot, "error_" + job.__name__,
                                       f"Автозадача «{name}» упала с ошибкой {type(e).__name__}: {e}"[:500])
                except Exception:
                    logger.exception("не удалось сообщить владельцу об ошибке автозадачи")
    return True


async def auto_scheduler(application):
    """Таймер автозадач, пока сервер не спит. Первая проверка через минуту после старта:
    бесплатный Render засыпает через 15 минут без запросов, и пропущенное за сон должно успеть пройти."""
    await asyncio.sleep(60)
    while True:
        await run_due_jobs(application.bot, "таймер")
        await asyncio.sleep(JOBS_EVERY)


def format_health(now=None):
    """Текст /health: когда что запускалось, без обращения к сайту вуза."""
    now = now or datetime.now(timezone.utc)
    msk = lambda moment: lms_sync._moscow_time(moment.isoformat()) if moment else "никогда"
    minutes = int((now - PROCESS_STARTED).total_seconds() // 60)
    lines = [f"Сервер запущен: {msk(PROCESS_STARTED)} ({minutes} мин назад), память {lms_sync.memory_mb() or '?'} МБ"]
    checked = job_status["checked_at"]
    lines.append(f"Проверка автозадач: {msk(checked)}" + (f" ({job_status['source']})" if checked else " с момента запуска"))
    lines.append("Вызов из GitHub раз в сутки: " + ("настроен (CRON_SECRET задан)" if os.environ.get("CRON_SECRET")
                                                    else "не настроен: нет CRON_SECRET в Render"))
    lines.append("")
    if not lms_sync.sync_enabled():
        state = "выключена (LMS_SYNC_ENABLED)"
    elif lms_sync.missing_settings(lms_sync.load_sources(SCHEDULE_DIR)):
        state = "не настроена: нет LMS_LOGIN или LMS_PASSWORD"
    elif not get_sync_state(conn, "last_run"):
        state = "ждёт первого ручного /sync"
    else:
        state = "включена"
    last_run, started = _utc(get_sync_state(conn, "last_run")), _utc(get_sync_state(conn, "lms_started_at"))
    ok = get_sync_state(conn, "last_ok")
    lines.append(f"Подгрузка: {state}")
    lines.append(f"Последняя: {msk(last_run)}" + ("" if not last_run else (", без проблем" if ok == "1" else ", с проблемами")))
    if started and (not last_run or started > last_run):
        lines.append(f"Начата и не закончена: {msk(started)}")
    lines.append(f"Резервная копия: {msk(_utc(get_sync_state(conn, 'backup_sent_at')))}"
                 + ("" if OWNER_ID.isdigit() else " (нет OWNER_ID, не отправляется)"))
    if job_status["error"]:
        lines += ["", "Последняя ошибка автозадач: " + lms_sync.scrub(job_status["error"])]
    return "\n".join(lines)


async def health_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(format_health())


# --- рассылка (только владелец) ---
BROADCAST_PAUSE = 0.05  # около 20 сообщений в секунду, лимит Telegram для рассылки - 30
BROADCAST_MAX_LEN = 4000


async def deliver_broadcast(bot, chat_ids, text, pause=BROADCAST_PAUSE, sleep=asyncio.sleep):
    """Отправляет text всем chat_ids по одному, с паузой. Возвращает (отправлено, не доставлено).
    Заблокировавшие бота и удалённые аккаунты пропускаются, на лимиты Telegram отвечаем ожиданием.
    С рассылкой приходит и постоянная клавиатура: так её получат и те, кто давно не нажимал /start."""
    sent = failed = 0
    for chat_id in chat_ids:
        for attempt in range(3):
            try:
                owner = OWNER_ID.isdigit() and str(chat_id) == OWNER_ID
                await bot.send_message(chat_id=chat_id, text=text, reply_markup=main_keyboard(owner))
                sent += 1
                break
            except RetryAfter as e:
                wait = e.retry_after
                await sleep((wait.total_seconds() if hasattr(wait, "total_seconds") else wait) + 1)
            except (Forbidden, BadRequest):
                failed += 1  # бот заблокирован, аккаунт удалён или чат недоступен
                break
            except (NetworkError, TelegramError):
                await sleep(2)
        else:
            failed += 1
        await sleep(pause)
    return sent, failed


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = update.message.text.split(None, 1)
    text = parts[1].strip() if len(parts) > 1 else ""
    if not text:
        await update.message.reply_text(
            "Напиши текст после команды: /broadcast Текст сообщения. Я покажу предпросмотр, "
            "и пока ты не нажмёшь «Отправить», никому ничего не уйдёт.")
        return
    if len(text) > BROADCAST_MAX_LEN:
        await update.message.reply_text(f"Слишком длинно: {len(text)} символов, максимум {BROADCAST_MAX_LEN}.")
        return
    recipients = [row[0] for row in list_users(conn)]
    token = secrets.token_hex(4)
    context.bot_data["broadcast"] = {"token": token, "text": text, "recipients": recipients}
    await update.message.reply_text(f"Предпросмотр. Так увидят пользователи:\n\n{text}")
    await update.message.reply_text(
        f"Получателей: {len(recipients)} (хранилище: {storage_name()}). Отправить?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(f"Отправить ({len(recipients)})", callback_data=f"bc:send:{token}"),
            InlineKeyboardButton("Отмена", callback_data=f"bc:cancel:{token}"),
        ]]))


async def broadcast_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not _is_owner(update):
        await answer_query(query)
        return
    _, action, token = query.data.split(":", 2)
    pending = context.bot_data.get("broadcast")
    if not pending or pending["token"] != token:
        await query.answer("Это сообщение уже обработано или заменено новым.", show_alert=True)
        return
    context.bot_data.pop("broadcast")  # повторное нажатие не отправит рассылку второй раз
    await answer_query(query)  # опоздавший ответ не должен сорвать уже снятую из очереди рассылку
    if action != "send":
        await query.edit_message_text("Рассылка отменена, ничего не отправлено.")
        return
    await query.edit_message_text(f"Отправляю {len(pending['recipients'])} получателям, отчёт пришлю сюда.")
    chat_id = query.message.chat_id

    async def run():
        sent, failed = await deliver_broadcast(context.bot, pending["recipients"], pending["text"])
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"Рассылка завершена. Отправлено: {sent}. Не доставлено: {failed} "
                 f"(заблокировали бота или удалили аккаунт).")

    context.application.create_task(run())


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(START_TEXT, reply_markup=keyboard_for(update))
    await group_command(update, context)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    telegram_id = update.effective_user.id

    # кнопка постоянной клавиатуры: то же, что команда (раньше всего: текст кнопки не пароль и не дата)
    action = keyboard_action(update, text)
    if action:
        context.user_data.pop("await_date", None)
        await run_keyboard_action(update, context, action)
        return

    # пароль от преподавателя для отметки посещаемости (/mark)
    if _is_owner(update) and context.bot_data.get("mark_password") and await mark_password_reply(update, context):
        return

    # после кнопки «По дате» следующее сообщение с датой показывает неделю
    if context.user_data.pop("await_date", False) and parse_input_date(text):
        context.args = [text]
        await weekdate(update, context)
        return

    # владелец может переслать боту сообщения /users: пользователи из них добавляются в базу
    if _is_owner(update):
        restored = parse_users_backup(text)
        if restored:
            added = restore_users(conn, restored)
            await update.message.reply_text(
                f"Восстановление: в сообщении {len(restored)} пользователей, добавлено новых {added} "
                f"(уже существующих не трогал).")
            return

    matches = find_groups(conn, text)
    if len(matches) > 1:
        # номер может совпадать на разных направлениях и курсах: сначала смотрим на текущий выбор
        current = get_user_group(conn, telegram_id)
        if current:
            same = [g for g in matches if (g.program, g.course) == (current.program, current.course)]
            matches = same or matches

    if len(matches) == 1:
        set_user_group(conn, telegram_id, matches[0])
        stats.count("pick.done")
        await update.message.reply_text(DONE_TEXT.format(title=matches[0].title), reply_markup=actions_markup())
    elif matches:
        await update.message.reply_text(
            "Такая группа есть на нескольких направлениях, выбери свою:",
            reply_markup=InlineKeyboardMarkup(_group_buttons(matches, per_row=1, with_program=True))
        )
    else:
        await update.message.reply_text(
            "Не понял. Нажми кнопку внизу или напиши номер группы. Команды тоже работают: /group, /week, /nweek, "
            "/weekdate, /app", reply_markup=keyboard_for(update)
        )


async def run_keyboard_action(update, context, action):
    context.args = []
    if action == "week":
        await week(update, context)
    elif action == "nweek":
        await nweek(update, context)
    elif action == "date":
        context.user_data["await_date"] = True
        await update.message.reply_text(DATE_PROMPT)
    elif action == "group":
        await group_command(update, context)
    elif action == "app":
        await app_command(update, context)
    elif action == "stats":
        await stats_command(update, context)
    elif action == "mark":
        await mark_command(update, context)
    elif action == "health":
        await health_command(update, context)


async def _send_week(update, group, week_sheet, not_found_msg):
    if not week_sheet:
        await update.message.reply_text(not_found_msg)
        return
    grid = get_week_schedule_grid(conn, group, week_sheet)
    text = with_donation_note(format_week_text(group.title, week_sheet, grid))
    await update.message.reply_text(text, parse_mode="MarkdownV2", reply_markup=open_app_markup(update))


def open_app_markup(update):
    """Кнопка «Открыть в приложении» под расписанием. Кнопки Mini App Telegram разрешает только в личке."""
    chat = getattr(update, "effective_chat", None) or getattr(getattr(update, "message", None), "chat", None)
    if chat is None or chat.type != "private":
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton("📱 Открыть в приложении", web_app=WebAppInfo(url=app_url("week")))]])


async def week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    group = get_user_group(conn, telegram_id)
    if not group:
        await update.message.reply_text(NO_GROUP_MSG)
        return
    week_sheet = get_current_week_sheet(conn, group)
    await _send_week(update, group, week_sheet, "Расписание не найдено.")


async def nweek(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    group = get_user_group(conn, telegram_id)
    if not group:
        await update.message.reply_text(NO_GROUP_MSG)
        return
    week_sheet = get_relative_week_sheet(conn, group, offset=1)
    await _send_week(update, group, week_sheet, "Расписание на следующую неделю пока не загружено.")


def parse_input_date(text):
    text = text.strip()
    formats = ["%d.%m.%Y", "%d.%m.%y", "%d.%m"]
    for fmt in formats:
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if "%Y" not in fmt and "%y" not in fmt:
            # без года - ближайшая к сегодняшнему дню такая дата: в декабре «10.01» - это январь следующего года
            today = now_local()
            candidates = []
            for year in (today.year - 1, today.year, today.year + 1):
                try:
                    candidates.append(dt.replace(year=year))
                except ValueError:
                    pass  # 29.02 не в високосный год
            dt = min(candidates, key=lambda d: abs(d - today), default=None)
        return dt
    return None


async def weekdate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    group = get_user_group(conn, telegram_id)
    if not group:
        await update.message.reply_text(NO_GROUP_MSG)
        return

    if not context.args:
        await update.message.reply_text("Укажи дату после команды, например: /weekdate 05.10")
        return

    target_date = parse_input_date(context.args[0])
    if target_date is None:
        await update.message.reply_text("Не понял дату. Формат: ДД.ММ или ДД.ММ.ГГГГ, например 05.10")
        return

    week_sheet = get_week_sheet_for_date(conn, group, target_date)
    await _send_week(update, group, week_sheet, "Расписание на эту дату не найдено.")


async def app_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Открыть расписание", web_app=WebAppInfo(url=app_url("app")))]
    ])
    await update.message.reply_text(
        "Нажми, чтобы открыть расписание:\n\n"
        "⚠️ Если страница долго грузится (первый раз за долгое время) — подожди секунд 30, сервер «просыпается».",
        reply_markup=keyboard
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # менять расписание может только владелец; без OWNER_ID загрузка файлов отключена
    if not _is_owner(update):
        return

    document = update.message.document
    if document.file_name.lower().endswith(".json"):
        # полная резервная копия (/backup или еженедельная): добавить то, чего нет в базе
        file = await context.bot.get_file(document.file_id)
        data = parse_backup(bytes(await file.download_as_bytearray()))
        if data is None:
            await update.message.reply_text("Это не резервная копия бота.")
            return
        added = import_backup(conn, data)
        await update.message.reply_text(
            "Восстановление из копии, добавлено новых: "
            + ", ".join(f"{BACKUP_TITLES[t]} {n}" for t, n in added.items()) + " (уже существующих не трогал).")
        return
    if document.file_name.lower().endswith((".csv", ".txt")):
        # файл /backup или текст списка /users: восстановить пользователей
        file = await context.bot.get_file(document.file_id)
        content = bytes(await file.download_as_bytearray()).decode("utf-8-sig", errors="replace")
        restored = parse_users_backup(content)
        if not restored:
            await update.message.reply_text("Не нашёл в файле пользователей.")
            return
        added = restore_users(conn, restored)
        await update.message.reply_text(
            f"Восстановление: в файле {len(restored)} пользователей, добавлено новых {added}.")
        return
    if not document.file_name.lower().endswith((".xlsx", ".xls")):
        await update.message.reply_text("Нужен файл в формате .xlsx или .xls")
        return

    await update.message.reply_text("Получил файл, обновляю расписание...")
    # чтение файла и перезагрузка расписания идут в фоне: вебхук должен ответить Telegram сразу,
    # иначе на большом файле он посчитает обновление потерянным и пришлёт его ещё раз
    context.application.create_task(_save_upload(update, context, document))


async def _save_upload(update, context, document):
    # файл кладётся в папку своего курса (data/1k, ...): с тем же именем заменяет старый, с новым добавляется
    os.makedirs(SCHEDULE_DIR, exist_ok=True)
    file_name = os.path.basename(document.file_name)
    tmp_path = os.path.join(SCHEDULE_DIR, f".upload-{file_name}")  # скрытые файлы не подхватываются

    try:
        file = await context.bot.get_file(document.file_id)
        await file.download_to_drive(tmp_path)
        # сначала проверяем, что файл читается, и только потом кладём его к остальным,
        # чтобы битый файл не ломал загрузку при следующем старте
        records = await asyncio.to_thread(parse_file, tmp_path, load_config(SCHEDULE_DIR))
        if not records:
            raise ValueError("не нашёл ни одного занятия")
        target_dir = upload_dir(SCHEDULE_DIR, file_name, records)
        os.makedirs(target_dir, exist_ok=True)
        os.replace(tmp_path, os.path.join(target_dir, file_name))
        records = await asyncio.to_thread(load_schedules)
        await update.message.reply_text(
            f"Готово, файл сохранён в {os.path.relpath(target_dir, os.path.dirname(SCHEDULE_DIR) or '.')}, "
            f"расписание обновлено ({len(records)} записей).\n{catalog_summary()}"
        )
    except Exception as e:
        await update.message.reply_text(f"Ошибка при обработке файла: {e}")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


CAMPUS_MAP_CAPTION = "Схема территории Российского университета спорта «ГЦОЛИФК»"
_campus_map_file_id = None  # после первой отправки Telegram отдаёт id файла: дальше картинку не загружаем заново


async def send_campus_map(bot, chat_id):
    """Схема вуза в чат пользователя (кнопка в мини-аппе): фото, текст на схеме крупный и после сжатия читается."""
    global _campus_map_file_id
    if _campus_map_file_id:
        await bot.send_photo(chat_id=chat_id, photo=_campus_map_file_id, caption=CAMPUS_MAP_CAPTION)
        return
    from routes import CAMPUS_MAP_FILE
    with open(CAMPUS_MAP_FILE, "rb") as f:
        message = await bot.send_photo(chat_id=chat_id, photo=f, caption=CAMPUS_MAP_CAPTION)
    _campus_map_file_id = message.photo[-1].file_id


async def post_init(application):
    await application.bot.set_my_commands([
        BotCommand("start", "Начать / указать группу"),
        BotCommand("app", "Открыть Mini App"),
        BotCommand("group", "Сменить направление или группу"),
        BotCommand("week", "Расписание на эту неделю"),
        BotCommand("nweek", "Расписание на следующую неделю"),
        BotCommand("weekdate", "Расписание на неделю по дате"),
    ])
    # кнопка слева от поля ввода: вместо списка команд открывает мини-апп; команды по-прежнему видны по «/»
    try:
        await application.bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="Расписание", web_app=WebAppInfo(url=app_url("menu"))))
    except TelegramError as e:
        logging.warning("Не удалось поставить кнопку меню: %s", e)


ERROR_TEXT = "Что-то пошло не так. Попробуй ещё раз чуть позже."
ERROR_NOTIFY_MINUTES = 30  # об одной и той же ошибке владельцу пишем не чаще
_error_notified = {}  # сигнатура ошибки -> когда владелец последний раз о ней узнал


def _is_stale_query(error):
    text = str(error).lower()
    return isinstance(error, BadRequest) and ("query is too old" in text or "query id is invalid" in text)


def _is_harmless(error):
    """Ошибки, о которых владельцу писать незачем: Telegram не дождался ответа (TimedOut), нажатие кнопки
    пришло после сна сервера (query is too old), повторное нажатие той же кнопки (message is not modified)."""
    return (isinstance(error, TimedOut) or _is_stale_query(error)
            or (isinstance(error, BadRequest) and "message is not modified" in str(error).lower()))


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    """Любая необработанная ошибка: в лог, пользователю короткий ответ вместо тишины, владельцу сообщение
    (одинаковые ошибки не чаще раза в ERROR_NOTIFY_MINUTES минут)."""
    error = context.error
    logger.error("ошибка при обработке обновления", exc_info=error)
    if isinstance(error, Forbidden):
        return  # пользователь заблокировал бота: ни ответить, ни сообщать нечего
    harmless = _is_harmless(error)
    if harmless and not isinstance(error, TimedOut):
        return  # экран и так показывает нужное: отвечать и сообщать нечего
    message = getattr(update, "effective_message", None)
    if message and message.chat.type == "private":
        stats.count("bot.error")
        try:
            await message.reply_text(ERROR_TEXT)
        except Exception:
            pass
    if harmless or not OWNER_ID.isdigit():
        return  # TimedOut: пользователю ответили (пусть повторит), владельцу не пишем, он есть в логе
    signature = f"{type(error).__name__}: {error}"[:200]
    now = datetime.now(timezone.utc)
    last = _error_notified.get(signature)
    if last and now - last < timedelta(minutes=ERROR_NOTIFY_MINUTES):
        return
    _error_notified[signature] = now
    user = getattr(update, "effective_user", None)
    where = f"от пользователя {user.id}" if user else "в фоновой задаче"
    text = lms_sync.scrub(f"Ошибка в боте ({where}):\n{signature}\n\nПовторы этой ошибки "
                          f"{ERROR_NOTIFY_MINUTES} мин присылать не буду, подробности в логах Render.")
    try:
        await context.bot.send_message(chat_id=int(OWNER_ID), text=text)
    except Exception:
        logger.exception("не удалось сообщить владельцу об ошибке")


def build_application():
    application = Application.builder().token(TOKEN).post_init(post_init).build()
    # только новые сообщения: правка уже отправленной команды или текста не должна запускать её ещё раз
    # (у правки нет update.message, и обработчик падал с ошибкой)
    new_message = filters.UpdateType.MESSAGE
    # group=-1: учёт пользователей идёт раньше остальных обработчиков и их не заменяет
    application.add_handler(TypeHandler(Update, track_user), group=-1)
    application.add_handler(CommandHandler("start", start, filters=new_message))
    application.add_handler(CommandHandler("group", group_command, filters=new_message))
    application.add_handler(CallbackQueryHandler(on_pick, pattern=r"^(l|p|c|g|b)(:|$)"))
    application.add_handler(CallbackQueryHandler(on_action, pattern=r"^a:"))
    application.add_handler(CommandHandler("week", week, filters=new_message))
    application.add_handler(CommandHandler("nweek", nweek, filters=new_message))
    application.add_handler(CommandHandler("weekdate", weekdate, filters=new_message))
    application.add_handler(CommandHandler("app", app_command, filters=new_message))
    if OWNER_ID.isdigit():
        # всем остальным команды владельца не отвечают вовсе: они нигде не публикуются
        owner = filters.User(user_id=int(OWNER_ID)) & new_message
        for command, callback in [("users", users_command), ("stats", stats_command), ("lms_check", lms_check_command),
                                  ("backup", backup_command), ("sync", sync_command), ("broadcast", broadcast_command),
                                  ("health", health_command), ("mark", mark_command)]:
            application.add_handler(CommandHandler(command, callback, filters=owner))
        application.add_handler(CallbackQueryHandler(broadcast_button, pattern=r"^bc:"))
        application.add_handler(CallbackQueryHandler(mark_button, pattern=r"^mk:"))
    application.add_handler(MessageHandler(filters.Document.ALL & new_message, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & new_message, handle_message))
    application.add_error_handler(on_error)
    return application


def main():
    """Локальный запуск через polling (для разработки на своём компьютере)."""
    application = build_application()
    application.run_polling()


if __name__ == "__main__":
    main()
