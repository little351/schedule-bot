"""Разбор файлов расписания.

Каждый формат файла - отдельный модуль с функциями detect(wb) и parse(wb, config).
Чтобы добавить новый формат: написать такой модуль и добавить его в PARSERS.
Формат файла определяется по его структуре, а не по имени.

Запись расписания (dict): program (код направления, напр. "49.03.04"), program_name, course,
group (название группы как в файле), week_sheet, day, date, time, subject.
"""
import logging
import os
import re

from . import grid_by_group, matrix_by_discipline
from .common import load_config
from .sheets import SPREADSHEET_EXTENSIONS, open_workbook

logger = logging.getLogger(__name__)

PARSERS = [grid_by_group, matrix_by_discipline]


class UnknownFormatError(ValueError):
    pass


def parse_file(path, config=None) -> list[dict]:
    wb = open_workbook(path)
    for parser in PARSERS:
        if parser.detect(wb):
            logger.info("%s: формат %s", os.path.basename(path), parser.NAME)
            return parser.parse(wb, config or {})
    raise UnknownFormatError(
        "не похоже ни на один известный формат расписания "
        "(нужны листы недель вида 01.09 и заголовок «День недели» или «Календарные сроки проведения»)"
    )


COURSE_DIRS = range(1, 6)  # data/1k ... data/5k
COURSE_IN_NAME_RE = re.compile(r"_(\d)k_")


def course_dir(schedule_dir, course) -> str:
    return os.path.join(schedule_dir, f"{course}k")


def upload_dir(schedule_dir, file_name, records) -> str:
    """Папка для нового файла: курс из имени (49.03.04_1k_...), иначе курс из самой таблицы;
    если курса нет среди 1-5, файл кладётся в корень data/."""
    m = COURSE_IN_NAME_RE.search(file_name)
    course = int(m.group(1)) if m else min((r["course"] for r in records if r.get("course")), default=None)
    return course_dir(schedule_dir, course) if course in COURSE_DIRS else schedule_dir


def list_schedule_files(schedule_dir) -> list[str]:
    """Все таблицы в папке и её подпапках по курсам (кроме временных файлов Excel '~$...' и скрытых),
    по пути в алфавитном порядке."""
    found = []
    for root, dirs, names in os.walk(schedule_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        found.extend(
            os.path.join(root, n) for n in names
            if n.lower().endswith(SPREADSHEET_EXTENSIONS) and not n.startswith(("~$", "."))
        )
    return sorted(found)


def parse_all(schedule_dir) -> list[dict]:
    """Расписание всех групп из всех файлов папки.

    Группа определяется тройкой (направление, курс, название). Если она встречается в нескольких
    файлах, побеждает файл, идущий позже по пути (папка курса, затем имя). Файл, который не удалось прочитать,
    пропускается и пишется в лог, остальные продолжают работать."""
    config = load_config(schedule_dir)
    by_file = []
    for path in list_schedule_files(schedule_dir):
        try:
            by_file.append((path, parse_file(path, config)))
        except Exception:
            logger.exception("не удалось прочитать файл расписания %s", path)

    source = {}
    for path, records in by_file:
        for r in records:
            source[(r["program"], r["course"], r["group"])] = path

    result = []
    for path, records in by_file:
        result.extend(r for r in records if source[(r["program"], r["course"], r["group"])] == path)
    return result
