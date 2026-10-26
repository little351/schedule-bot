"""Кеш разобранных файлов data/: старт бота берёт готовые записи, а не разбирает таблицы заново."""
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import lms_sync  # noqa: E402
import schedule_parsers  # noqa: E402
from schedule_parsers import list_schedule_files  # noqa: E402

DATA = os.path.join(ROOT, "data")


def test_committed_cache_is_fresh():
    """Упал этот тест - поменялись файлы в data/, парсеры или programs.json: выполни `python parser.py --build-cache`."""
    cache = lms_sync.load_parsed_cache(DATA)
    assert cache, "кеша нет или он собран другими парсерами"
    stale = [os.path.relpath(p, DATA) for p in list_schedule_files(DATA) if lms_sync._file_sha(p) not in cache]
    assert not stale, f"файлов нет в кеше: {stale}"


def test_cache_gives_same_records_and_new_files_are_parsed(tmp_path):
    files = list_schedule_files(DATA)[:2]
    for path in files:
        shutil.copy(path, tmp_path / os.path.basename(path))
    shutil.copy(os.path.join(DATA, "programs.json"), tmp_path / "programs.json")
    fresh = lms_sync.disk_records(str(tmp_path))  # кеша нет: всё разбирается
    assert fresh

    lms_sync.build_parsed_cache(str(tmp_path))
    calls = []
    real = schedule_parsers.parse_file
    schedule_parsers.parse_file = lambda *a, **k: calls.append(a) or real(*a, **k)
    try:
        cached = lms_sync.disk_records(str(tmp_path))
        assert calls == []  # всё из кеша
        assert [(r["group"], r["week_sheet"], r["day"], str(r["date"]), r["time"], r["subject"]) for r in cached] == \
               [(r["group"], r["week_sheet"], r["day"], str(r["date"]), r["time"], r["subject"]) for r in fresh]
        extra = list_schedule_files(DATA)[2]
        shutil.copy(extra, tmp_path / os.path.basename(extra))  # новый файл (например, загруженный владельцем)
        assert len(lms_sync.disk_records(str(tmp_path))) > len(cached)
        assert calls  # файла нет в кеше: таблицы разбираются, как раньше
    finally:
        schedule_parsers.parse_file = real
