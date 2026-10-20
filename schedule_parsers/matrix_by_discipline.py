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
# части ячейки преподавателя: «л - Иванов п - Петров», «1 гр - Иванов, 2 гр - Петров», «5,6 гр. - Иванов»
_PART_KEY = r"(?:л|п|\d+(?:\s*,\s*\d+)*\s*гр\.?)"
TEACHER_PART_RE = re.compile(
    rf"({_PART_KEY})\s*-\s*(.*?)(?=[,;]?\s+{_PART_KEY}\s*-\s|$)", re.IGNORECASE | re.DOTALL
)
MARKER_PREFIX = {"л": "Л.", "п": "Пр."}
PRACTICE_SLOT = "Практика"  # вместо времени пары: практика идёт весь день
PERIOD_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{2,4})\D+?(\d{2})\.(\d{2})\.(\d{2,4})")


def _period(text):
    """'26.11.26 - 14.12.26 гг.' / 'с 26.11.26 по 14.12.26' -> (дата начала, дата конца) или None."""
    m = PERIOD_RE.search(text)
    if not m:
        return None
    d1, m1, y1, d2, m2, y2 = m.groups()
    year = lambda y: int(y) + 2000 if len(y) == 2 else int(y)
    try:
        return datetime.datetime(year(y1), int(m1), int(d1)), datetime.datetime(year(y2), int(m2), int(d2))
    except ValueError:
        return None


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


def _teacher_for(teacher, marker, group_numbers=()):
    """Преподаватель для пары. 'л - Иванов  п - Петров' -> по отметке; '1 гр - А, 2 гр - Б' -> по номеру
    группы (group_numbers в порядке предпочтения); без таких частей строка остаётся как есть."""
    teacher = re.sub(r"\s+", " ", teacher).strip()
    parts = {}
    for key, value in TEACHER_PART_RE.findall(teacher):
        key = re.sub(r"\s+|\.", "", key.lower())  # '5 гр.' -> '5гр'
        for number in re.findall(r"\d+", key) if key.endswith("гр") else [None]:
            parts[f"{number}гр" if number else key] = value.strip(" ,;")  # '5,6гр' -> '5гр' и '6гр'
    if not parts:
        return teacher
    own = next((parts[f"{n}гр"] for n in group_numbers if f"{n}гр" in parts), None)
    chosen = (parts.get("л") or own) if marker == "л" else (own or parts.get("п"))
    return chosen or ", ".join(v for v in parts.values() if v)


_DAY_PREFIXES = {"пн": 0, "пон": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5}
_DAYS = r"(?:пн|пон|вт|ср|чт|пт|сб)"
# метка в ячейке аудитории: «пн -», «ср,пт -», «л -», «п -», «2 п -»
_ROOM_SELECTOR_RE = re.compile(
    rf"(?:(?<=\s)|(?<=,)|^)((?:{_DAYS}\.?(?:\s*,\s*{_DAYS}\.?)*)|[лп]|\d\s*п\.?)\s*[-–]\s*", re.IGNORECASE
)


def _room_for(room, marker, weekday, slot):
    """Аудитория для пары. Ячейка может перечислять аудитории по дням («пн - 512 гл.к. пт - 508 гл.к.»),
    по виду занятия («л - 218 ауд.к. п - 327 мбк») или по номеру пары («2 п - 70 мбк, 3 п - 161 мбк»);
    тогда берём подходящую, остальные в строке не нужны. Что не удалось разобрать, остаётся как в таблице."""
    room = re.sub(r"\s+", " ", room).strip()
    found = list(_ROOM_SELECTOR_RE.finditer(room))
    if not found:
        return room
    default = room[:found[0].start()].strip(" ,;")
    segments = []  # (вид, значение, текст)
    for i, m in enumerate(found):
        end = found[i + 1].start() if i + 1 < len(found) else len(room)
        label = re.sub(r"[\s.]", "", m.group(1).lower())
        text = room[m.end():end].strip(" ,;")
        if label in ("л", "п"):
            segments.append(("marker", label, text))
        elif label[:-1].isdigit() and label.endswith("п"):
            segments.append(("slot", int(label[:-1]), text))
        else:
            days = {_DAY_PREFIXES[d] for d in re.findall(_DAYS, label)}
            segments.append(("days", days, text))
    if len({kind for kind, _, _ in segments}) > 1 or not all(text for _, _, text in segments):
        return room  # день вместе с номером пары и т.п.: не гадаем
    wanted = {"marker": marker, "slot": slot, "days": weekday}
    matched = [text for kind, value, text in segments
               if (weekday in value if kind == "days" else value == wanted[kind])]
    if matched:
        return " ".join(matched)
    return default or room


def _group_numbers(code, position):
    """Номера, по которым группу называют в ячейке преподавателя: суффикс '/2', номер в коде, место в шапке."""
    numbers = []
    m = re.search(r"/(\d)(?:-\d{2})?$", code)
    if m:
        numbers.append(int(m.group(1)))
    m = re.search(r"-(\d+)(?:/\d)?-\d{2}", code)
    if m:
        numbers.append(int(m.group(1)))
    numbers.append(position)
    return numbers


def _row_groups(index, groups):
    """Какой группе (группам) принадлежит строка. В столбце «группа» стоит номер группы
    (5 для СГ1-5-26, 2 для Т2-07-26/2) или порядковый номер в шапке; пусто - строка общая."""
    positions = list(enumerate(groups, 1))
    if len(groups) == 1:
        return positions
    if index.isdigit():
        n = int(index)
        by_number = [(pos, g) for pos, g in positions if n in _group_numbers(g, 0)[:-1]]
        if len(by_number) == 1:
            return by_number
        if 1 <= n <= len(groups):
            return [positions[n - 1]]
    return positions


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
            elif h == "группа":
                col_of["group"] = c
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
        # коды групп из шапки; в файле их может быть несколько (строки по группам идут по очереди)
        groups = list(meta.group_codes)
        if not groups:
            m = GROUP_CODE_RE.search(" ".join(t for _, _, t in sheet.cells(15)))
            groups = [m.group(0)] if m else []
        if not groups:
            continue
        program = (meta.programs[0][0] if meta.programs else None) or resolver.program_for_code(groups[0]) or ""
        slot_times = _slot_times(sheet)

        found = {}  # (группа, день, дата, время) -> [предметы]
        current = None  # дисциплина, к которой относятся строки групп
        for r in range(slot_row + 1, sheet.max_row + 1):
            number = sheet.text(r, 1)
            name = sheet.text(r, col_of["name"])
            index = sheet.text(r, col_of["group"]) if "group" in col_of else ""
            if number and not number.isdigit():
                break  # дальше легенда «Условные обозначения»
            room = sheet.text(r, col_of["room"]) if "room" in col_of else ""
            teacher = sheet.text(r, col_of["teacher"]) if "teacher" in col_of else ""
            if name:
                current = {"name": name, "room": room, "teacher": teacher}
                row = current
            elif current is None or not index:
                continue  # пустая строка между дисциплинами
            else:
                # строка другой группы той же дисциплины: свои преподаватель и аудитория, если указаны
                row = {"name": current["name"], "room": room or current["room"], "teacher": teacher or current["teacher"]}
            row_groups = _row_groups(index, groups)

            for day, date, slots in day_cols:
                for col, slot in slots.items():
                    marker = sheet.text(r, col).lower().rstrip(".")
                    if marker not in MARKER_PREFIX:
                        continue
                    for position, group in row_groups:
                        teacher = _teacher_for(row["teacher"], marker, _group_numbers(group, position))
                        room_text = _room_for(row["room"], marker, DAY_NAMES.index(day), slot)
                        subject = " ".join(filter(None, [f"{MARKER_PREFIX[marker]} {row['name']}", teacher, room_text]))
                        found.setdefault((group, day, date, slot_times.get(slot, "")), []).append(subject)

        # практика: в столбцах дней вместо отметки «л»/«п» стоит текст («Производственная практика: ...
        # - с 26.11.26 по 14.12.26 гг.»); срок берётся из него, без срока - только день, где стоит ячейка
        practice = {}  # текст -> {столбец}
        for r in range(slot_row + 1, sheet.max_row + 1):
            if sheet.text(r, 1) and not sheet.text(r, 1).isdigit():
                break
            for _, _, slots in day_cols:
                for c in slots:
                    text = re.sub(r"\s+", " ", sheet.text(r, c))
                    if "практик" in text.lower():
                        practice.setdefault(text, set()).add(c)
        for text, cols in practice.items():
            period = _period(text)
            label = re.sub(r"[\s(–-]*с?\s*\d{2}\.\d{2}\.\d{2,4}.*$", "", text).strip(" -–(") or text
            if period:
                label += f" ({period[0]:%d.%m}–{period[1]:%d.%m})"
            for day, date, slots in day_cols:
                if period and date:
                    active = period[0] <= date <= period[1]
                else:
                    active = bool(cols & set(slots))
                if active:
                    for group in groups:
                        found.setdefault((group, day, date, PRACTICE_SLOT), []).append(label)

        for (group, day, date, time), subjects in found.items():
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
