"""Общие вещи для парсеров: сведения о направлении, курсе и группах из шапки листа, настройки."""
import json
import logging
import os
import re

logger = logging.getLogger(__name__)

WEEK_SHEET_RE = re.compile(r"^\d{2}\.\d{2}$")  # листы недель вида "01.09"
DAY_NAMES = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
DEFAULT_SLOT_TIMES = {
    1: "8.50-10.20", 2: "10.40-12.10", 3: "13.00-14.30",
    4: "14.50-16.20", 5: "16.40-18.10", 6: "18.30-20.00", 7: "20.20-21.50",
}

# 49.03.04 Спорт, ... / 49.03.02 Физическая культура для лиц ... (адаптивная ...) профиль ...
PROGRAM_RE = re.compile(r"(\d{2}\.\d{2}\.\d{2})\s+(.+?)(?=\s*(?:[,;(*\n]|[Пп]рофиль|$))", re.DOTALL)
GROUPS_PREFIX_RE = re.compile(r"\bгрупп[аы]?\s+(?=[А-ЯA-Z]{1,2}\d)", re.IGNORECASE)
COURSE_RE = re.compile(r"[Кк]урс\s+(\d+)")
# подгруппа может стоять в конце (Т2-07-26/2) или после номера группы (Т1-12/1-26)
GROUP_CODE_RE = re.compile(r"[А-ЯA-Z]{1,2}\d-\d{1,2}[А-Я]?(?:/\d)?-\d{2}(?:/\d)?")
# группы магистратуры пишут по-разному: «МП-01-26», «МП 25-26», «МП - 14.1- 26», «МП - 14.2.-26»
MASTER_CODE_RE = re.compile(r"\bМП\s*-?\s*(\d{1,2}(?:\.\d)?)\.?\s*-\s*(\d{2})\b")


def master_codes(text) -> list[str]:
    """Коды групп магистратуры из текста, в одном виде: 'МП - 14.1- 26' -> 'МП-14.1-26'."""
    return [f"МП-{number}-{year}" for number, year in MASTER_CODE_RE.findall(text)]


def normalize_group(name) -> str:
    """'13 группа   ' -> '13 группа'"""
    return re.sub(r"\s+", " ", str(name)).strip()


class SheetMeta:
    """То, что написано в шапке листа: направления, курс, коды групп."""

    def __init__(self, programs, course, group_codes):
        self.programs = programs          # [(код, название), ...] в порядке следования
        self.course = course              # int или None
        self.group_codes = group_codes    # ['С2-07-26', 'Т1-04-26', ...]


def extract_meta(sheet, max_rows=15) -> SheetMeta:
    programs, course, group_codes = [], None, []
    for _, _, text in sheet.cells(max_rows):
        for code, name in PROGRAM_RE.findall(text):
            if code not in [p[0] for p in programs]:
                programs.append((code, re.sub(r"\s+", " ", name).strip()))
        if course is None:
            m = COURSE_RE.search(text)
            if m:
                course = int(m.group(1))
        # «группа СГ1-5-26, СГ1-6-26» может стоять отдельной ячейкой или в конце строки с направлением
        m = GROUPS_PREFIX_RE.search(text)
        if m:
            group_codes.extend(c for c in GROUP_CODE_RE.findall(text[m.end():]) if c not in group_codes)
        if "групп" in text.lower():
            group_codes.extend(c for c in master_codes(text) if c not in group_codes)
    return SheetMeta(programs, course, group_codes)


def load_config(schedule_dir) -> dict:
    """Необязательный data/programs.json: названия направлений и соответствие групп направлениям.

    {
      "programs": {"49.03.04": "Спорт", "49.03.01": "Физическая культура"},
      "group_prefixes": {"С": "49.03.04", "Т1": "49.03.01", "Т2": "49.03.02"},
      "default_course": 1
    }
    """
    path = os.path.join(schedule_dir, "programs.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        logger.exception("не удалось прочитать %s", path)
        return {}


class Resolver:
    """Определяет направление и курс для групп листа по шапке и настройкам."""

    def __init__(self, meta: SheetMeta, config: dict):
        self.meta = meta
        self.config = config or {}
        names = dict(meta.programs)
        names.update(self.config.get("programs", {}))
        self.program_names = names

    @property
    def course(self):
        return self.meta.course or int(self.config.get("default_course", 1))

    def program_name(self, code):
        return self.program_names.get(code, code)

    def program_for_code(self, group_code):
        """Направление по коду группы (С2-07-26): сначала настройки по префиксу, потом шапка."""
        prefixes = self.config.get("group_prefixes", {})
        head = group_code.split("-")[0]
        for key in (head, head[:1]):
            if key in prefixes:
                return prefixes[key]
        return None

    def program_for_header(self, header):
        """Направление для столбца с заголовком '7 группа' или 'Т1-04-26'."""
        programs = [p[0] for p in self.meta.programs]
        if len(programs) == 1:
            return programs[0]

        code = None
        if GROUP_CODE_RE.fullmatch(header):
            code = header
        else:
            m = re.match(r"(\d+)", header)
            if m:
                number = int(m.group(1))
                for c in self.meta.group_codes:
                    mc = re.match(r"[^-]+-(\d+)", c)
                    if mc and int(mc.group(1)) == number:
                        code = c
                        break
        program = self.program_for_code(code) if code else None
        if program:
            return program
        if programs:
            logger.warning("не удалось определить направление группы %r, беру %s "
                           "(можно указать в data/programs.json)", header, programs[0])
            return programs[0]
        return self.config.get("default_program", "")
