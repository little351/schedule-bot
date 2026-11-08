"""Статистика действий: только счётчики, без текстов (заметки, поиск и сообщения не сохраняются).

Счётчики копятся в памяти сервера и раз в 10 минут (и при остановке) одним запросом на таблицу уходят в базу:
- stats_daily(day, metric, value): одна строка на счётчик в день, например 2026-11-09 | cmd.week | 412;
- stats_seen(day, telegram_id, kinds): одна строка на человека в день, kinds - где он был (бот, мини-апп, игра).
Читается всё это только по /stats и для сводки по понедельникам, и в ответ приходят уже готовые числа."""
import csv
import io
import logging
import threading
from collections import Counter
from datetime import datetime, timedelta

import database
from database import now_local

logger = logging.getLogger(__name__)

FLUSH_EVERY = 600  # секунд между записями в базу
SEEN_BOT, SEEN_APP, SEEN_GAME = 1, 2, 4
KEEP_SEEN_DAYS = 180  # дольше строки по людям сворачиваются в дневные итоги

# что мини-апп может прислать в /api/track (остальное отбрасывается, чтобы в базу не попадал мусор)
APP_EVENTS = {
    "tab.schedule", "tab.important", "tab.other",
    "other.group", "other.notes", "other.teachers", "other.random", "other.game", "other.map",
    "about.open", "day.swipe", "day.today", "teachers.search", "teachers.copy", "donate.copy",
}
APP_EVENT_MAX = 500  # больше за одну отправку от одного человека не бывает
APP_SOURCES = ("menu", "week", "pick", "app")  # откуда открыли мини-апп: метка ?src= в адресе кнопки

_lock = threading.Lock()
_counts = Counter()  # (день, счётчик) -> число
_seen = {}  # (день, telegram_id) -> kinds


def _day(moment=None):
    return (moment or now_local()).strftime("%Y-%m-%d")


def count(metric, n=1):
    """Прибавить к счётчику за сегодня (по Москве). Ничего не читает и не пишет в базу."""
    if n <= 0:
        return
    with _lock:
        _counts[(_day(), metric)] += n


def seen(telegram_id, kind):
    """Человек сегодня был в боте / мини-аппе / игре. Заодно считается час активности (по Москве)."""
    if not telegram_id:
        return
    now = now_local()
    with _lock:
        key = (_day(now), int(telegram_id))
        _seen[key] = _seen.get(key, 0) | kind
        if kind != SEEN_GAME:
            _counts[(_day(now), f"hour.{now.hour:02d}")] += 1


def app_source(src):
    return src if src in APP_SOURCES else "other"


def app_platform(platform):
    """Telegram.WebApp.platform -> android / ios / desktop (компьютер и веб-версии)."""
    platform = (platform or "").lower()
    if platform == "android" or platform.startswith("android"):
        return "android"
    if platform == "ios":
        return "ios"
    if platform in ("tdesktop", "macos", "web", "weba", "webk", "unigram"):
        return "desktop"
    return "other"


def pending():
    with _lock:
        return len(_counts) + len(_seen)


def flush(conn):
    """Записать накопленное в базу. При ошибке всё возвращается в память и уйдёт со следующей записью."""
    with _lock:
        counts, seen_rows = dict(_counts), dict(_seen)
        _counts.clear()
        _seen.clear()
    if not counts and not seen_rows:
        return 0
    try:
        database.stats_write(conn, [(d, m, v) for (d, m), v in counts.items()],
                             [(d, t, k) for (d, t), k in seen_rows.items()])
    except Exception:
        with _lock:
            for key, value in counts.items():
                _counts[key] += value
            for key, kinds in seen_rows.items():
                _seen[key] = _seen.get(key, 0) | kinds
        raise
    return len(counts) + len(seen_rows)


# --- отчёт ---

PLATFORMS = [("android", "Android"), ("ios", "iOS"), ("desktop", "ПК"), ("other", "другое")]
SOURCES = [("menu", "кнопка «Расписание»"), ("week", "под расписанием"), ("pick", "после выбора группы"),
           ("app", "/app"), ("other", "прочее")]
COMMANDS = ["start", "week", "nweek", "weekdate", "app", "group"]
BUTTONS = [("btn.week", "неделя"), ("btn.nweek", "след. неделя"), ("btn.date", "по дате"), ("btn.group", "сменить группу")]
TABS = [("tab.schedule", "Расписание"), ("tab.important", "Важное"), ("tab.other", "Другое")]
OTHER = [("other.group", "Моя группа"), ("other.notes", "заметки"), ("other.teachers", "преподаватели"),
         ("other.random", "рандомайзер"), ("other.game", "«Блоки»"), ("other.map", "карта")]


def _n(value):
    return f"{value:,}".replace(",", " ")


def _pct(part, whole):
    return f"{round(100 * part / whole)}%" if whole else "0%"


def _join(pairs, totals):
    return " · ".join(f"{label} {_n(totals.get(key, 0))}" for key, label in pairs)


def report_data(conn, days=7, now=None):
    """Всё для отчёта за последние days дней (включая сегодня): несколько запросов с готовыми суммами."""
    today = (now or now_local()).date()
    start = today - timedelta(days=days - 1)
    data = database.stats_read(conn, start.isoformat(), today.isoformat(),
                               (today - timedelta(days=13)).isoformat(), (today - timedelta(days=6)).isoformat(),
                               (today - timedelta(days=29)).isoformat())
    data.update(days=days, start=start, today=today, live=now is None)
    return data


def format_report(data, title=None):
    t, by_day = data["totals"], data["by_day"]
    today, start, days = data["today"], data["start"], data["days"]
    if not by_day and not t:
        return "📊 Статистика действий: данных пока нет, они начнут копиться с первыми действиями пользователей."
    first = data.get("first_day")
    title = title or f"📊 Статистика за {days} дн. ({start:%d.%m}–{today:%d.%m})"
    lines = [title, ""]
    live = data.get("live", True)
    day_label = "Сегодня" if live else f"{today:%d.%m}"
    prev_label = "вчера" if live else f"{today - timedelta(days=1):%d.%m}"
    td = by_day.get(today.isoformat(), (0, 0, 0, 0))
    yd = by_day.get((today - timedelta(days=1)).isoformat(), (0, 0, 0, 0))
    lines.append("Люди")
    lines.append(f"  {day_label} {_n(td[0])} (бот {_n(td[1])}, мини-апп {_n(td[2])}) · {prev_label} {_n(yd[0])}")
    windows = [] if days in (7, 30) else [f"за {days} дн. {_n(data['uniq_period'])}"]
    windows += [f"за 7 дн. {_n(data['uniq_7'])}", f"за 30 дн. {_n(data['uniq_30'])}"]
    lines.append("  " + (" · ".join(windows)).capitalize())
    cohort, returned = data["retention"]
    if first and first <= (today - timedelta(days=13)).isoformat() and cohort:
        lines.append(f"  Из активных неделей раньше вернулись на этой неделе: {_pct(returned, cohort)}")
    lines.append("")

    opens = sum(t.get(f"open.{key}", 0) for key, _ in SOURCES)
    lines.append(f"Входы в мини-апп: {_n(opens)}")
    lines.append("  " + " · ".join(f"{label} {_n(t.get('open.' + key, 0))}" for key, label in SOURCES))
    plat_total = sum(t.get(f"platform.{key}", 0) for key, _ in PLATFORMS)
    if plat_total:
        lines.append("  Платформы: " + ", ".join(f"{label} {_pct(t.get('platform.' + key, 0), plat_total)}"
                                                for key, label in PLATFORMS if t.get("platform." + key)))
    lines.append("")

    lines.append("Бот")
    lines.append("  " + " · ".join(f"/{c} {_n(t.get('cmd.' + c, 0))}" for c in COMMANDS))
    lines.append("  Кнопки: " + _join(BUTTONS, t))
    started, done = t.get("pick.start", 0), t.get("pick.done", 0)
    lines.append(f"  Выбор группы: начат {_n(started)}, выбрана группа {_n(done)}")
    lines.append(f"  Ошибки «Что-то пошло не так»: {_n(t.get('bot.error', 0))}")
    lines.append("")

    lines.append("Мини-апп")
    lines.append("  Вкладки: " + _join(TABS, t))
    lines.append("  Другое: " + _join(OTHER, t))
    lines.append(f"  Листание дней {_n(t.get('day.swipe', 0))} · «сегодня» {_n(t.get('day.today', 0))} · "
                 f"смена группы {_n(t.get('app.group', 0))}")
    lines.append(f"  Заметки: создано {_n(t.get('note.add', 0))}, удалено {_n(t.get('note.delete', 0))}")
    lines.append(f"  Преподаватели: поиск {_n(t.get('teachers.search', 0))}, "
                 f"копирование контактов {_n(t.get('teachers.copy', 0))}")
    lines.append(f"  Копирование номера для доната {_n(t.get('donate.copy', 0))} · «Авторы» {_n(t.get('about.open', 0))}")
    lines.append("")

    skins = sorted(((m[5:], v) for m, v in t.items() if m.startswith("skin.")), key=lambda kv: -kv[1])
    lines.append("Игра «Блоки»")
    lines.append(f"  Партий {_n(t.get('game.played', 0))} (сложный режим {_n(t.get('game.played_hard', 0))}) · "
                 f"игроков {_n(data['players'])} · очков {_n(t.get('game.points', 0))}")
    lines.append(f"  Куплено скинов {_n(sum(v for _, v in skins))}"
                 + (" (" + ", ".join(f"{name} {v}" for name, v in skins) + ")" if skins else ""))

    hours = [(int(m[5:]), v) for m, v in t.items() if m.startswith("hour.") and m[5:].isdigit()]
    total_hours = sum(v for _, v in hours)
    if total_hours:
        top = sorted(hours, key=lambda hv: -hv[1])[:3]
        lines.append("")
        lines.append("Пик активности (МСК): " + ", ".join(f"{h}–{h + 1} ({_pct(v, total_hours)})" for h, v in top))
    if first and first > start.isoformat():
        lines += ["", f"Данные собираются с {datetime.fromisoformat(first):%d.%m.%Y}."]
    return "\n".join(lines)


def format_csv(rows, by_day):
    """CSV по дням для Excel: люди за день и все счётчики столбцами (разделитель «;», как ждёт русский Excel)."""
    metrics = sorted({metric for _, metric, _ in rows})
    table = {}
    for day, metric, value in rows:
        table.setdefault(day, {})[metric] = value
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";")
    writer.writerow(["день", "люди", "бот", "мини-апп", "игра"] + metrics)
    for day in sorted(set(table) | set(by_day)):
        people = by_day.get(day, (0, 0, 0, 0))
        writer.writerow([day, *people] + [table.get(day, {}).get(m, 0) for m in metrics])
    return "﻿" + out.getvalue()  # BOM: без него Excel показывает кириллицу кракозябрами
