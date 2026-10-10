"""Совместимый вход для остального кода; сами парсеры лежат в пакете schedule_parsers."""
from schedule_parsers import (  # noqa: F401
    UnknownFormatError, list_schedule_files, parse_all, parse_file,
)

parse_schedule = parse_file
parse_all_schedules = parse_all


if __name__ == "__main__":
    from collections import Counter

    data = parse_all("data")
    print(f"Всего записей: {len(data)}")
    counts = Counter((r["program"], r["course"], r["group"]) for r in data)
    for (program, course, group), n in sorted(counts.items()):
        print(f"{program} курс {course} {group}: {n}")
