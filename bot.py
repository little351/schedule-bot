import csv
import io
import logging
import os
import re
from datetime import datetime
from telegram import Update, BotCommand, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, MessageHandler, TypeHandler, ContextTypes, filters
)

from parser import parse_file, parse_all_schedules
from schedule_parsers import upload_dir
from schedule_parsers.common import load_config
from database import (
    init_db, set_user_group, get_user_group, save_schedule,
    get_current_week_sheet, get_relative_week_sheet, get_week_sheet_for_date,
    get_week_schedule_grid, get_today_day_name, now_local, get_groups, get_catalog, find_groups, GroupRef, storage_name,
    touch_user, list_users, restore_users, users_without_profile, set_user_profile
)
from formatting import format_week_text, format_day_text

TOKEN = os.environ.get("BOT_TOKEN") or ""
WEBAPP_URL = os.environ.get("WEBAPP_URL") or os.environ.get("PUBLIC_URL") or "https://breeding-salvation-splashing.ngrok-free.dev"
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
    records = parse_all_schedules(SCHEDULE_DIR)
    save_schedule(conn, records)
    return records


def catalog_summary():
    """Что сейчас загружено: по строке на направление, группы по курсам."""
    lines = []
    for program in get_catalog(conn):
        by_course = ", ".join(f"{c['course']} курс {len(c['groups'])}" for c in program["courses"])
        lines.append(f"{program['code']} {program['name']}: групп {by_course}")
    return "\n".join(lines) if lines else "расписание пока не загружено"


NO_GROUP_MSG = "Сначала выбери направление и группу: /group"


START_TEXT = (
    "Привет! Я показываю расписание.\n\n"
    "Как пользоваться:\n"
    "1. Выбери курс, направление и группу кнопками ниже. Сменить их можно в любой момент: /group "
    "(или просто напиши номер группы).\n"
    "2. /app — открыть расписание в мини-приложении.\n"
    "3. Расписание прямо в чате: /day — сегодня, /week — эта неделя, "
    "/nweek — следующая, /weekdate 05.10 — неделя по дате.\n\n"
    "⚠️ Бот неофициальный: расписание загружается вручную и может измениться или содержать "
    "неточности, поэтому сверяйся с официальным. Бот работает на бесплатном хостинге: после простоя "
    "он может отвечать с задержкой (до ~30 секунд), а иногда быть временно недоступен. "
    "Если ничего не происходит — подожди и повтори."
)

DONE_TEXT = (
    "Готово, запомнил: {title}.\n"
    "/app — расписание в мини-приложении, /day, /week, /nweek, /weekdate — расписание в чате, "
    "/group — сменить группу."
)


# --- выбор курса, направления и группы кнопками ---
# callback_data: c:<курс> | p:<курс>:<направление> | g:<направление>:<курс>:<группа>
#                b:c (назад к курсам) | b:<курс> (назад к направлениям курса)

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


def picker_screen(step="course", course=None, program=None):
    """Текст и клавиатура очередного шага: курс -> направление -> группа.
    Шаг направления пропускается, если на курсе одно направление."""
    catalog = get_catalog(conn)
    if not catalog:
        return "Расписание пока не загружено, попробуй позже.", None
    courses = _courses(catalog)

    if step == "course" or course not in courses:
        buttons = [InlineKeyboardButton(f"{c} курс", callback_data=f"c:{c}") for c in courses]
        return "Выбери курс:", InlineKeyboardMarkup([buttons[i:i + 3] for i in range(0, len(buttons), 3)])

    programs = _programs_of_course(catalog, course)
    if step == "program" and len(programs) > 1:
        rows = [[InlineKeyboardButton(f"{p['code']} {p['name']}", callback_data=f"p:{course}:{p['code']}")]
                for p in programs]
        return f"{course} курс. Выбери направление подготовки:", InlineKeyboardMarkup(rows + _back_row("c"))

    entry = next((p for p in programs if p["code"] == program), programs[0])
    groups = get_groups(conn, entry["code"], course)
    back = str(course) if len(programs) > 1 else "c"
    return (f"{course} курс, {entry['code']} {entry['name']}. Выбери группу:",
            InlineKeyboardMarkup(_group_buttons(groups) + _back_row(back)))


async def group_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, markup = picker_screen()
    await update.message.reply_text(text, reply_markup=markup)


async def on_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split(":", 3)

    if parts[0] == "g" and len(parts) == 4 and parts[2].isdigit():
        group = GroupRef(parts[1], int(parts[2]), parts[3])
        if group not in get_groups(conn):
            await query.edit_message_text("Такой группы нет в расписании. Попробуй /group ещё раз.")
            return
        set_user_group(conn, query.from_user.id, group)
        await query.edit_message_text(DONE_TEXT.format(title=group.title))
        return

    if parts[0] == "c" and len(parts) >= 2 and parts[1].isdigit():
        text, markup = picker_screen("program", course=int(parts[1]))
    elif parts[0] == "p" and len(parts) >= 3 and parts[1].isdigit():
        text, markup = picker_screen("groups", course=int(parts[1]), program=parts[2])
    elif parts[0] == "b" and len(parts) >= 2 and parts[1].isdigit():
        text, markup = picker_screen("program", course=int(parts[1]))
    else:
        text, markup = picker_screen()
    await query.edit_message_text(text, reply_markup=markup)


async def track_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Срабатывает на каждое обращение, ничего не отвечает и не мешает остальным обработчикам."""
    user = update.effective_user
    if not user or user.is_bot:
        return
    try:
        touch_user(conn, user.id, user.username, user.full_name)
    except Exception:
        # учёт не должен ломать работу бота, но ошибку нужно видеть в логах
        logger.exception("не удалось записать пользователя %s", user.id)


def format_users_messages(rows):
    """Список пользователей, разбитый на сообщения короче лимита Telegram (4096)."""
    header = f"Пользователей: {len(rows)}"
    lines = []
    for telegram_id, username, first_name, group_name, first_seen, last_seen, program, course in rows:
        handle = f"@{username}" if username else "(без username)"
        last = (last_seen or "?")[:10]
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


# строка списка /users: «@ivan · Иван · 4 группа (49.03.04, 2 курс) · 2026-10-09 · id 123»;
# в старом формате курса нет: «4 группа (49.03.04)», такие пользователи получают 1 курс
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
        last = None if last == "?" else last
        first_name = None if first_name == "—" else first_name
        rows.append((int(telegram_id), username or None, first_name, group, last, last, program, course))
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


async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = format_users_csv(list_users(conn))
    name = f"users-{datetime.now().strftime('%Y-%m-%d')}.csv"
    await update.message.reply_document(document=io.BytesIO(data), filename=name,
                                        caption="Резервная копия списка пользователей")


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await backfill_profiles(context.bot)
    rows = list_users(conn)
    await update.message.reply_text(f"Хранилище пользователей: {storage_name()}")
    for text in format_users_messages(rows):
        await update.message.reply_text(text)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(START_TEXT)
    await group_command(update, context)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    telegram_id = update.effective_user.id

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
        await update.message.reply_text(DONE_TEXT.format(title=matches[0].title))
    elif matches:
        await update.message.reply_text(
            "Такая группа есть на нескольких направлениях, выбери свою:",
            reply_markup=InlineKeyboardMarkup(_group_buttons(matches, per_row=1, with_program=True))
        )
    else:
        await update.message.reply_text(
            "Не понял. Выбери группу через /group или используй /week, /nweek, /day, /weekdate, /app"
        )


async def _send_week(update, group, week_sheet, not_found_msg):
    if not week_sheet:
        await update.message.reply_text(not_found_msg)
        return
    grid = get_week_schedule_grid(conn, group, week_sheet)
    text = format_week_text(group.title, week_sheet, grid)
    await update.message.reply_text(text, parse_mode="MarkdownV2")


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
            if "%Y" not in fmt and "%y" not in fmt:
                dt = dt.replace(year=now_local().year)
            return dt
        except ValueError:
            continue
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


async def day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    group = get_user_group(conn, telegram_id)
    if not group:
        await update.message.reply_text(NO_GROUP_MSG)
        return

    week_sheet = get_current_week_sheet(conn, group)
    day_name = get_today_day_name()
    grid = get_week_schedule_grid(conn, group, week_sheet)
    text = format_day_text(group.title, week_sheet, day_name, grid)

    await update.message.reply_text(text, parse_mode="MarkdownV2")


async def app_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Открыть расписание", web_app=WebAppInfo(url=WEBAPP_URL))]
    ])
    await update.message.reply_text(
        "Нажми, чтобы открыть расписание:\n\n"
        "⚠️ Если страница долго грузится (первый раз за долгое время) — подожди секунд 30, сервер «просыпается».",
        reply_markup=keyboard
    )

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # менять расписание может только владелец; без OWNER_ID загрузка файлов отключена
    if not OWNER_ID.isdigit() or update.effective_user.id != int(OWNER_ID):
        return

    document = update.message.document
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

    # файл кладётся в папку своего курса (data/1k, ...): с тем же именем заменяет старый, с новым добавляется
    file = await context.bot.get_file(document.file_id)
    os.makedirs(SCHEDULE_DIR, exist_ok=True)
    file_name = os.path.basename(document.file_name)
    tmp_path = os.path.join(SCHEDULE_DIR, f".upload-{file_name}")  # скрытые файлы не подхватываются
    await file.download_to_drive(tmp_path)

    try:
        # сначала проверяем, что файл читается, и только потом кладём его к остальным,
        # чтобы битый файл не ломал загрузку при следующем старте
        records = parse_file(tmp_path, load_config(SCHEDULE_DIR))
        if not records:
            raise ValueError("не нашёл ни одного занятия")
        target_dir = upload_dir(SCHEDULE_DIR, file_name, records)
        os.makedirs(target_dir, exist_ok=True)
        os.replace(tmp_path, os.path.join(target_dir, file_name))
        records = load_schedules()
        await update.message.reply_text(
            f"Готово, файл сохранён в {os.path.relpath(target_dir, os.path.dirname(SCHEDULE_DIR) or '.')}, "
            f"расписание обновлено ({len(records)} записей).\n{catalog_summary()}"
        )
    except Exception as e:
        await update.message.reply_text(f"Ошибка при обработке файла: {e}")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


async def post_init(application):
    await application.bot.set_my_commands([
        BotCommand("start", "Начать / указать группу"),
        BotCommand("group", "Сменить направление или группу"),
        BotCommand("week", "Расписание на эту неделю"),
        BotCommand("nweek", "Расписание на следующую неделю"),
        BotCommand("day", "Расписание на сегодня"),
        BotCommand("weekdate", "Расписание на неделю по дате"),
        BotCommand("app", "Открыть Mini App"),
    ])


def build_application():
    application = Application.builder().token(TOKEN).post_init(post_init).build()
    # group=-1: учёт пользователей идёт раньше остальных обработчиков и их не заменяет
    application.add_handler(TypeHandler(Update, track_user), group=-1)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("group", group_command))
    application.add_handler(CallbackQueryHandler(on_pick, pattern=r"^(p|c|g|b)(:|$)"))
    application.add_handler(CommandHandler("week", week))
    application.add_handler(CommandHandler("nweek", nweek))
    application.add_handler(CommandHandler("day", day))
    application.add_handler(CommandHandler("weekdate", weekdate))
    application.add_handler(CommandHandler("app", app_command))
    if OWNER_ID.isdigit():
        # всем остальным /users не отвечает вовсе: команда нигде не публикуется
        application.add_handler(CommandHandler("users", users_command, filters=filters.User(user_id=int(OWNER_ID))))
        application.add_handler(CommandHandler("backup", backup_command, filters=filters.User(user_id=int(OWNER_ID))))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return application


def main():
    """Локальный запуск через polling (для разработки на своём компьютере)."""
    application = build_application()
    application.run_polling()


if __name__ == "__main__":
    main()
