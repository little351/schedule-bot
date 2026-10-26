"""Совместимый вход для остального кода; сами парсеры лежат в пакете schedule_parsers."""
from schedule_parsers import parse_all, parse_file

parse_schedule = parse_file
parse_all_schedules = parse_all


if __name__ == "__main__":
    import sys
    from collections import Counter

    if "--build-cache" in sys.argv:
        # после любой правки файлов в data/, парсеров или data/programs.json (тест проверяет, что кеш свежий)
        from lms_sync import build_parsed_cache
        print(f"Кеш собран: файлов {build_parsed_cache('data')}")
        sys.exit()

    data = parse_all("data")
    print(f"Всего записей: {len(data)}")
    counts = Counter((r["program"], r["course"], r["group"]) for r in data)
    for (program, course, group), n in sorted(counts.items()):
        print(f"{program} курс {course} {group}: {n}")
