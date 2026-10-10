"""Разбор файлов расписания.

Каждый формат файла - отдельный модуль с функциями detect(wb) и parse(wb, config).
Чтобы добавить новый формат: написать такой модуль и добавить его в PARSERS.
Формат файла определяется по его структуре, а не по имени.

Запись расписания (dict): program (код направления, напр. "49.03.04"), program_name, course,
group (название группы как в файле), week_sheet, day, date, time, subject.
"""
import logging
import os

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


def list_schedule_files(schedule_dir) -> list[str]:
    """Все таблицы в папке (кроме временных файлов Excel '~$...' и скрытых), по алфавиту."""
    names = os.listdir(schedule_dir) if os.path.isdir(schedule_dir) else []
    return sorted(
        os.path.join(schedule_dir, n) for n in names
        if n.lower().endswith(SPREADSHEET_EXTENSIONS) and not n.startswith(("~$", "."))
    )


def parse_all(schedule_dir) -> list[dict]:
    """Расписание всех групп из всех файлов папки.

    Группа определяется тройкой (направление, курс, название). Если она встречается в нескольких
    файлах, побеждает файл, идущий позже по алфавиту. Файл, который не удалось прочитать,
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
