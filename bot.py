import asyncio
import csv
import io
import logging
import os
import re
import secrets
from datetime import datetime
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError
from telegram import Update, BotCommand, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, MessageHandler, TypeHandler, ContextTypes, filters
)

import lms_sync
from parser import parse_file
from schedule_parsers import upload_dir
from schedule_parsers.common import load_config
from database import (
    init_db, set_user_group, get_user_group, save_schedule,
    get_current_week_sheet, get_relative_week_sheet, get_week_sheet_for_date,
    get_week_schedule_grid, get_today_day_name, now_local, get_groups, get_catalog, find_groups, GroupRef, storage_name,
    touch_user, list_users, user_stats, get_program_name, restore_users, users_without_profile, set_user_profile,
    get_sync_state, set_sync_state
)
from formatting import format_week_text, format_day_text, with_donation_note

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
    # порядок источников: свежая подгрузка с сайта вуза, затем загруженное владельцем, затем файлы репозитория
    try:
        records = lms_sync.merged_records(conn, SCHEDULE_DIR)
    except Exception:
        logger.exception("не удалось учесть подгруженные файлы, беру только data/")
        records = lms_sync.disk_records(SCHEDULE_DIR)
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
        lines += ["", "По курсам:"]
        lines += [f"{c} курс: {n}" for c, n in sorted(stats["courses"].items(), key=lambda kv: (kv[0] is None, kv[0]))]
        lines += ["", "По направлениям:"]
        lines += [f"{program_name(p)} ({p}): {n}"
                  for p, n in sorted(stats["programs"].items(), key=lambda kv: (-kv[1], kv[0] or ""))]
        lines += ["", "Популярные группы:"]
        lines += [f"{name} ({p}, {c} курс): {n}" for (p, c, name), n in stats["top_groups"]]
    return "\n".join(lines)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = user_stats(list_users(conn))
    await update.message.reply_text(f"Хранилище пользователей: {storage_name()}\n\n"
                                    + format_stats(stats, lambda code: get_program_name(conn, code)))


async def lms_check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Диагностика доступа к сайту вуза: на каком шаге обрывается вход. Пароль в ответ не попадает."""
    import lms_check
    do_login = bool(context.args) and context.args[0].lower() == "login"
    await update.message.reply_text("Проверяю доступ к сайту вуза, это займёт до минуты...")
    lines = await asyncio.to_thread(
        lms_check.run_checks, login=os.environ.get("LMS_LOGIN", "").strip(),
        password=os.environ.get("LMS_PASSWORD", ""), do_login=do_login)
    await update.message.reply_text("\n".join(lines))




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
        lms_sync.record_run(conn, report)
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


async def lms_scheduler(application):
    """Фоновая подгрузка: проверяет раз в полчаса, пора ли, и запускает по расписанию.
    О сбое сообщает владельцу, повторно об одном и том же не чаще раза в LMS_FAIL_REPEAT_DAYS."""
    await asyncio.sleep(600)
    while True:
        try:
            # плановые запуски начинаются после первого ручного /sync владельца: так видно, что всё работает
            if lms_sync.sync_enabled() and get_sync_state(conn, "last_run") and lms_sync.is_due(conn) \
                    and not lms_sync.missing_settings(lms_sync.load_sources(SCHEDULE_DIR)):
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
                            await application.bot.send_message(chat_id=int(OWNER_ID), text=part)
                elif not report.problems:
                    set_sync_state(conn, "notified_signature", "")
        except Exception:
            logger.exception("плановая подгрузка расписания: ошибка")
        await asyncio.sleep(1800)

# --- рассылка (только владелец) ---
BROADCAST_PAUSE = 0.05  # около 20 сообщений в секунду, лимит Telegram для рассылки - 30
BROADCAST_MAX_LEN = 4000


async def deliver_broadcast(bot, chat_ids, text, pause=BROADCAST_PAUSE, sleep=asyncio.sleep):
    """Отправляет text всем chat_ids по одному, с паузой. Возвращает (отправлено, не доставлено).
    Заблокировавшие бота и удалённые аккаунты пропускаются, на лимиты Telegram отвечаем ожиданием."""
    sent = failed = 0
    for chat_id in chat_ids:
        for attempt in range(3):
            try:
                await bot.send_message(chat_id=chat_id, text=text)
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
        await query.answer()
        return
    _, action, token = query.data.split(":", 2)
    pending = context.bot_data.get("broadcast")
    if not pending or pending["token"] != token:
        await query.answer("Это сообщение уже обработано или заменено новым.", show_alert=True)
        return
    context.bot_data.pop("broadcast")  # повторное нажатие не отправит рассылку второй раз
    await query.answer()
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
    text = with_donation_note(format_week_text(group.title, week_sheet, grid))
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
        application.add_handler(CommandHandler("stats", stats_command, filters=filters.User(user_id=int(OWNER_ID))))
        application.add_handler(CommandHandler("lms_check", lms_check_command, filters=filters.User(user_id=int(OWNER_ID))))
        application.add_handler(CommandHandler("backup", backup_command, filters=filters.User(user_id=int(OWNER_ID))))
        application.add_handler(CommandHandler("sync", sync_command, filters=filters.User(user_id=int(OWNER_ID))))
        application.add_handler(CommandHandler("broadcast", broadcast_command, filters=filters.User(user_id=int(OWNER_ID))))
        application.add_handler(CallbackQueryHandler(broadcast_button, pattern=r"^bc:"))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return application


def main():
    """Локальный запуск через polling (для разработки на своём компьютере)."""
    application = build_application()
    application.run_polling()


if __name__ == "__main__":
    main()
