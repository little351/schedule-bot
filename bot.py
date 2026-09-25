import os
from datetime import datetime
from telegram import Update, BotCommand, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

from parser import parse_schedule
from database import (
    init_db, set_user_group, get_user_group, save_schedule,
    get_current_week_sheet, get_relative_week_sheet, get_week_sheet_for_date,
    get_week_schedule_grid, get_today_day_name
)
from formatting import format_week_text, format_day_text

TOKEN = os.environ.get("BOT_TOKEN") or ""
WEBAPP_URL = os.environ.get("WEBAPP_URL") or os.environ.get("PUBLIC_URL") or "https://breeding-salvation-splashing.ngrok-free.dev"
SCHEDULE_PATH = "data/schedule.xlsx"

conn = init_db()

GROUPS = ["10", "11", "12", "13", "14", "15", "16"]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Напиши номер своей группы (10-16), чтобы я запомнил его.\n\n"
        ""
        "⚠️ Бот работает на бесплатном хостинге и может «засыпать» при простое. "
        "Если после команды или открытия приложения ничего не происходит — подожди секунд 30 и попробуй ещё раз."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    telegram_id = update.effective_user.id

    if text in GROUPS:
        set_user_group(conn, telegram_id, f"{text} группа")
        await update.message.reply_text(
            f"Готово, запомнил группу {text}. Команды: /app - основная команда, /week, /nweek, /day, /weekdate - используются для сохранения расписания в чате"
        )
    else:
        await update.message.reply_text(
            "Не понял. Введи номер группы от 10 до 16, либо /week, /nweek, /day, /weekdate, /app"
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
        await update.message.reply_text("Сначала укажи группу — просто напиши её номер (10-16).")
        return
    week_sheet = get_current_week_sheet(conn)
    await _send_week(update, group, week_sheet, "Расписание не найдено.")


async def nweek(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    group = get_user_group(conn, telegram_id)
    if not group:
        await update.message.reply_text("Сначала укажи группу — просто напиши её номер (10-16).")
        return
    week_sheet = get_relative_week_sheet(conn, offset=1)
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
        await update.message.reply_text("Сначала укажи группу — просто напиши её номер (10-16).")
        return

    if not context.args:
        await update.message.reply_text("Укажи дату после команды, например: /weekdate 05.10")
        return

    target_date = parse_input_date(context.args[0])
    if target_date is None:
        await update.message.reply_text("Не понял дату. Формат: ДД.ММ или ДД.ММ.ГГГГ, например 05.10")
        return

    week_sheet = get_week_sheet_for_date(conn, target_date)
    await _send_week(update, group, week_sheet, "Расписание на эту дату не найдено.")


async def day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    group = get_user_group(conn, telegram_id)
    if not group:
        await update.message.reply_text("Сначала укажи группу — просто напиши её номер (10-16).")
        return

    week_sheet = get_current_week_sheet(conn)
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

    file = await context.bot.get_file(document.file_id)
    os.makedirs(os.path.dirname(SCHEDULE_PATH), exist_ok=True)
    await file.download_to_drive(SCHEDULE_PATH)

    try:
        records = parse_schedule(SCHEDULE_PATH)
        save_schedule(conn, records)
        await update.message.reply_text(f"Готово, расписание обновлено ({len(records)} записей).")
    except Exception as e:
        await update.message.reply_text(f"Ошибка при обработке файла: {e}")


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
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("week", week))
    application.add_handler(CommandHandler("nweek", nweek))
    application.add_handler(CommandHandler("day", day))
    application.add_handler(CommandHandler("weekdate", weekdate))
    application.add_handler(CommandHandler("app", app_command))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return application


def main():
    """Локальный запуск через polling (для разработки на своём компьютере)."""
    application = build_application()
    application.run_polling()


if __name__ == "__main__":
    main()