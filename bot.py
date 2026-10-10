import logging
import os
from datetime import datetime
from telegram import (
    Update, BotCommand, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo,
    ReplyKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, TypeHandler, ContextTypes, filters
)

from parser import parse_schedule, parse_all_schedules
from database import (
    init_db, set_user_group, get_user_group, save_schedule,
    get_current_week_sheet, get_relative_week_sheet, get_week_sheet_for_date,
    get_week_schedule_grid, get_today_day_name, get_groups, group_short_name, find_group,
    touch_user, list_users, users_without_profile, set_user_profile
)
from formatting import format_week_text, format_day_text

TOKEN = os.environ.get("BOT_TOKEN") or ""
WEBAPP_URL = os.environ.get("WEBAPP_URL") or os.environ.get("PUBLIC_URL") or "https://breeding-salvation-splashing.ngrok-free.dev"
# все .xlsx из этой папки загружаются как расписание; группы берутся из заголовков листов
SCHEDULE_DIR = "data"

logger = logging.getLogger(__name__)

# Telegram ID владельца: только он может смотреть список пользователей (/users)
OWNER_ID = os.environ.get("OWNER_ID", "").strip()

conn = init_db()


def load_schedules():
    records = parse_all_schedules(SCHEDULE_DIR)
    save_schedule(conn, records)
    return records


def groups_hint():
    names = [group_short_name(g) for g in get_groups(conn)]
    return ", ".join(names) if names else "расписание пока не загружено"


def groups_keyboard():
    names = [group_short_name(g) for g in get_groups(conn)]
    rows = [names[i:i + 4] for i in range(0, len(names), 4)]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True) if rows else None


NO_GROUP_MSG = "Сначала укажи группу — просто напиши её номер ({})."


START_TEXT = (
    "Привет! Я показываю расписание 1 курса.\n\n"
    "Как пользоваться:\n"
    "1. Выбери свою группу кнопкой ниже или напиши её номер — я запомню. "
    "Сменить группу можно в любой момент, просто написав другой номер.\n"
    "2. /app — открыть расписание в мини-приложении.\n"
    "3. Расписание прямо в чате: /day — сегодня, /week — эта неделя, "
    "/nweek — следующая, /weekdate 05.10 — неделя по дате.\n\n"
    "⚠️ Бот неофициальный: расписание загружается вручную и может измениться или содержать "
    "неточности, поэтому сверяйся с официальным. Бот работает на бесплатном хостинге: после простоя "
    "он может отвечать с задержкой (до ~30 секунд), а иногда быть временно недоступен. "
    "Если ничего не происходит — подожди и повтори.\n\n"
    "Для статистики бот сохраняет твой username и выбранную группу."
)


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
    for telegram_id, username, first_name, group_name, first_seen, last_seen in rows:
        handle = f"@{username}" if username else "(без username)"
        last = (last_seen or "?")[:10]
        lines.append(f"{handle} · {first_name or '—'} · {group_name or 'группа не выбрана'} · {last} · id {telegram_id}")

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


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await backfill_profiles(context.bot)
    rows = list_users(conn)
    for text in format_users_messages(rows):
        await update.message.reply_text(text)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        START_TEXT,
        reply_markup=groups_keyboard()
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    telegram_id = update.effective_user.id

    group = find_group(conn, text)
    if group:
        set_user_group(conn, telegram_id, group)
        await update.message.reply_text(
            f"Готово, запомнил: {group}. Команды: /app - основная команда, /week, /nweek, /day, /weekdate - используются для сохранения расписания в чате",
            reply_markup=ReplyKeyboardRemove()
        )
    else:
        await update.message.reply_text(
            f"Не понял. Введи номер группы ({groups_hint()}), либо /week, /nweek, /day, /weekdate, /app"
        )


async def _send_week(update, group, week_sheet, not_found_msg):
    if not week_sheet:
        await update.message.reply_text(not_found_msg)
        return
    grid = get_week_schedule_grid(conn, group, week_sheet)
    text = format_week_text(group, week_sheet, grid)
    await update.message.reply_text(text, parse_mode="MarkdownV2")


async def week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    group = get_user_group(conn, telegram_id)
    if not group:
        await update.message.reply_text(NO_GROUP_MSG.format(groups_hint()))
        return
    week_sheet = get_current_week_sheet(conn, group)
    await _send_week(update, group, week_sheet, "Расписание не найдено.")


async def nweek(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    group = get_user_group(conn, telegram_id)
    if not group:
        await update.message.reply_text(NO_GROUP_MSG.format(groups_hint()))
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
                dt = dt.replace(year=datetime.now().year)
            return dt
        except ValueError:
            continue
    return None


async def weekdate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    group = get_user_group(conn, telegram_id)
    if not group:
        await update.message.reply_text(NO_GROUP_MSG.format(groups_hint()))
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
        await update.message.reply_text(NO_GROUP_MSG.format(groups_hint()))
        return

    week_sheet = get_current_week_sheet(conn, group)
    day_name = get_today_day_name()
    grid = get_week_schedule_grid(conn, group, week_sheet)
    text = format_day_text(group, week_sheet, day_name, grid)

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
    document = update.message.document
    if not document.file_name.endswith(".xlsx"):
        await update.message.reply_text("Нужен файл в формате .xlsx")
        return

    await update.message.reply_text("Получил файл, обновляю расписание...")

    # файл с тем же именем заменяет старый, с новым именем - добавляется к остальным
    file = await context.bot.get_file(document.file_id)
    os.makedirs(SCHEDULE_DIR, exist_ok=True)
    file_name = os.path.basename(document.file_name)
    target_path = os.path.join(SCHEDULE_DIR, file_name)
    tmp_path = os.path.join(SCHEDULE_DIR, f".upload-{file_name}")  # скрытые файлы не подхватываются
    await file.download_to_drive(tmp_path)

    try:
        # сначала проверяем, что файл читается, и только потом кладём его к остальным,
        # чтобы битый файл не ломал загрузку при следующем старте
        if not parse_schedule(tmp_path):
            raise ValueError("не нашёл ни одного занятия (листы недель вида 01.09, заголовок 'День недели')")
        os.replace(tmp_path, target_path)
        records = load_schedules()
        await update.message.reply_text(
            f"Готово, расписание обновлено ({len(records)} записей). Группы: {groups_hint()}."
        )
    except Exception as e:
        await update.message.reply_text(f"Ошибка при обработке файла: {e}")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


async def post_init(application):
    await application.bot.set_my_commands([
        BotCommand("start", "Начать / указать группу"),
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
    application.add_handler(CommandHandler("week", week))
    application.add_handler(CommandHandler("nweek", nweek))
    application.add_handler(CommandHandler("day", day))
    application.add_handler(CommandHandler("weekdate", weekdate))
    application.add_handler(CommandHandler("app", app_command))
    if OWNER_ID.isdigit():
        # всем остальным /users не отвечает вовсе: команда нигде не публикуется
        application.add_handler(CommandHandler("users", users_command, filters=filters.User(user_id=int(OWNER_ID))))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return application


def main():
    """Локальный запуск через polling (для разработки на своём компьютере)."""
    application = build_application()
    application.run_polling()


if __name__ == "__main__":
    main()