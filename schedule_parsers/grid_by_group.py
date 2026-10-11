"""Формат «сетка по группам»: на листе недели строки - дни и пары, столбцы - группы.

    День недели | Дата | Время | 10 группа | 11 группа | ...
    Понедельник | дата | 8.50-10.20 | предмет | предмет | ...
                |      | 10.40-12.10 | ...

Число пар в дне не фиксировано (у некоторых дней есть 6-я пара 18.30-20.00).
"""
import re

from .common import (
    DAY_NAMES, WEEK_SHEET_RE, Resolver, extract_meta, normalize_group,
)

NAME = "grid_by_group"
HEADER_MARKER = "День недели"
FIRST_GROUP_COL = 4  # столбец D
TIME_RE = re.compile(r"^\d{1,2}\.\d{2}-\d{1,2}\.\d{2}$")


def _header_row(sheet):
    for row in range(1, 21):
        if sheet.text(row, 1) == HEADER_MARKER:
            return row
    return None


def detect(wb) -> bool:
    return any(WEEK_SHEET_RE.match(s.name) and _header_row(s) for s in wb.sheets)


def parse(wb, config) -> list[dict]:
    records = []
    for sheet in wb.sheets:
        if not WEEK_SHEET_RE.match(sheet.name):
            continue  # служебные листы
        header_row = _header_row(sheet)
        if not header_row:
            continue

        resolver = Resolver(extract_meta(sheet), config)

        # группы: все непустые столбцы заголовка начиная с D; заголовок «6 группа, Т1-04-25» - это
        # две группы, которые учатся вместе, поэтому одно расписание записывается каждой из них
        groups = {}
        for col in range(FIRST_GROUP_COL, sheet.max_col + 1):
            header = sheet.text(header_row, col)
            if header:
                names = [normalize_group(part) for part in header.split(",") if part.strip()]
                groups[col] = [(resolver.program_for_header(name), name) for name in names]

        day_name = date_val = None
        for r in range(header_row + 1, sheet.max_row + 1):
            a_val = sheet.text(r, 1)
            if a_val in DAY_NAMES:
                day_name = a_val
                date_val = sheet.as_datetime(r, 2)
            if not day_name:
                continue  # строка с направлениями под заголовком

            time_val = sheet.text(r, 3)
            if not TIME_RE.match(time_val):
                continue

            for col, col_groups in groups.items():
                subject = sheet.text_merged(r, col)  # лекция на несколько групп - одна объединённая ячейка
                if not subject:
                    continue
                for program, name in col_groups:
                    records.append({
                        "program": program,
                        "program_name": resolver.program_name(program),
                        "course": resolver.course,
                        "group": name,
                        "week_sheet": sheet.name,
                        "day": day_name,
                        "date": date_val,
                        "time": time_val,
                        "subject": subject,
                    })
    return records
