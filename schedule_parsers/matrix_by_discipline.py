"""Формат «матрица по дисциплинам»: одна группа на файл, строки - дисциплины,
столбцы - пары каждого дня недели, на пересечении отметка «л» (лекция) или «п» (семинар).

    № | Дисциплина | Часы | Отчётность | Аудитория | Преподаватель | группа | Календарные сроки проведения
                                                                              дата (по 5 столбцов на день)
                                                                              день недели
                                                                              1 2 3 4 5 | 1 2 3 4 5 | ... | л | п
    1 | Иностранный язык | 0/32 | б/о | 507 гл.к. | Ходырева И.Н. |   |     п
Название группы, направление и курс берутся из строки шапки:
«... направление подготовки ... 49.03.02 ... курс 1 ... группа Т2-09-26».
"""
import datetime
import re

from .common import (
    DAY_NAMES, DEFAULT_SLOT_TIMES, GROUP_CODE_RE, WEEK_SHEET_RE, Resolver, extract_meta,
)

NAME = "matrix_by_discipline"
CALENDAR_MARKER = "календарные сроки"
LEGEND_RE = re.compile(r"(\d)\s*пара\s*(\d{1,2})[:.](\d{2})\s*-\s*(\d{1,2})[:.](\d{2})")
TEACHER_PART_RE = re.compile(r"([лп])\s*-\s*(.*?)(?=\s+[лп]\s*-\s|$)", re.IGNORECASE | re.DOTALL)
MARKER_PREFIX = {"л": "Л.", "п": "Пр."}


def _find(sheet, predicate, max_rows=40):
    for r, c, text in sheet.cells(max_rows):
        if predicate(text):
            return r, c
    return None


def detect(wb) -> bool:
    return any(
        WEEK_SHEET_RE.match(s.name) and _find(s, lambda t: CALENDAR_MARKER in t.lower())
        for s in wb.sheets
    )


def _slot_times(sheet):
    """Время пар из легенды под таблицей: «1 пара 08:50-10:20; 2 пара ...» -> {1: '8.50-10.20'}."""
    times = dict(DEFAULT_SLOT_TIMES)
    for _, _, text in sheet.cells():
        for slot, h1, m1, h2, m2 in LEGEND_RE.findall(text):
            times[int(slot)] = f"{int(h1)}.{m1}-{int(h2)}.{m2}"
    return times


def _teacher_for(teacher, marker):
    """'л - Иванов  п - Петров' -> для «п» 'Петров'; без отметок строка остаётся как есть."""
    teacher = re.sub(r"\s+", " ", teacher).strip()
    parts = {m.lower(): v.strip() for m, v in TEACHER_PART_RE.findall(teacher)}
    if not parts:
        return teacher
    return parts.get(marker) or ", ".join(v for v in parts.values() if v)


def parse(wb, config) -> list[dict]:
    records = []
    for sheet in wb.sheets:
        if not WEEK_SHEET_RE.match(sheet.name):
            continue

        cal = _find(sheet, lambda t: CALENDAR_MARKER in t.lower())
        if not cal:
            continue
        header_row = cal[0]

        # столбцы с названием дисциплины, аудиторией и преподавателем - по заголовкам
        col_of = {}
        for c in range(1, sheet.max_col + 1):
            h = sheet.text(header_row, c).lower()
            if h.startswith("наименование"):
                col_of["name"] = c
            elif h.startswith("аудитория"):
                col_of["room"] = c
            elif h.startswith("ф.и.о"):
                col_of["teacher"] = c
        if "name" not in col_of:
            continue

        # строка с названиями дней недели и блоки по 5 столбцов (пар) под ними
        day_row = next(
            (r for r in range(header_row, header_row + 6)
             if any(sheet.text(r, c).capitalize() in DAY_NAMES for c in range(1, sheet.max_col + 1))),
            None,
        )
        if not day_row:
            continue
        date_row, slot_row = day_row - 1, day_row + 1

        starts = [c for c in range(1, sheet.max_col + 1) if sheet.text(day_row, c).capitalize() in DAY_NAMES]
        day_cols = []  # (день, дата, {столбец: номер пары})
        for i, c in enumerate(starts):
            end = starts[i + 1] if i + 1 < len(starts) else sheet.max_col + 1
            slots = {}
            for cc in range(c, end):
                label = sheet.text(slot_row, cc)
                if not label.isdigit():
                    break  # дальше столбцы итогов «л» и «п» или пусто
                slots[cc] = int(label)
            day_cols.append((sheet.text(day_row, c).capitalize(), sheet.as_datetime(date_row, c), slots))

        # у дня без даты (например, понедельник в неполной неделе) дату берём по соседним дням
        known = next(((DAY_NAMES.index(d), dt) for d, dt, _ in day_cols if dt), None)
        if known:
            day_cols = [
                (d, dt or known[1] + datetime.timedelta(days=DAY_NAMES.index(d) - known[0]), slots)
                for d, dt, slots in day_cols
            ]

        meta = extract_meta(sheet)
        resolver = Resolver(meta, config)
        # в таком файле одна группа: её код написан в шапке
        group = meta.group_codes[0] if meta.group_codes else None
        if not group:
            m = GROUP_CODE_RE.search(" ".join(t for _, _, t in sheet.cells(15)))
            group = m.group(0) if m else None
        if not group:
            continue
        program = (meta.programs[0][0] if meta.programs else None) or resolver.program_for_code(group) or ""
        slot_times = _slot_times(sheet)

        found = {}  # (день, дата, время) -> [предметы]
        for r in range(slot_row + 1, sheet.max_row + 1):
            number = sheet.text(r, 1)
            name = sheet.text(r, col_of["name"])
            if not name or (number and not number.isdigit()):
                break  # дальше легенда «Условные обозначения»
            room = sheet.text(r, col_of["room"]) if "room" in col_of else ""
            teacher = sheet.text(r, col_of["teacher"]) if "teacher" in col_of else ""

            for day, date, slots in day_cols:
                for col, slot in slots.items():
                    marker = sheet.text(r, col).lower().rstrip(".")
                    if marker not in MARKER_PREFIX:
                        continue
                    subject = " ".join(filter(None, [
                        f"{MARKER_PREFIX[marker]} {name}", _teacher_for(teacher, marker), room,
                    ]))
                    found.setdefault((day, date, slot_times.get(slot, "")), []).append(subject)

        for (day, date, time), subjects in found.items():
            if not time:
                continue
            records.append({
                "program": program,
                "program_name": resolver.program_name(program),
                "course": resolver.course,
                "group": group,
                "week_sheet": sheet.name,
                "day": day,
                "date": date,
                "time": time,
                "subject": " / ".join(subjects),  # две дисциплины в одну пару - редкость, показываем обе
            })
    return records
