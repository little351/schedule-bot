import re
from openpyxl import load_workbook

DAYS_PER_WEEK = 6          # Пн-Сб
SLOTS_PER_DAY = 5          # 5 пар в день
FIRST_DAY_ROW = 9          # первая строка первого дня (Понедельник)
GROUP_COLS = "DEFGHIJ"     # столбцы групп 10-16

WEEK_SHEET_RE = re.compile(r"^\d{2}\.\d{2}$")  # листы вида "01.09"


def parse_schedule(file_path: str) -> list[dict]:
    """Возвращает список записей:
    {week_sheet, day, date, time, group, subject}
    """
    wb = load_workbook(file_path, data_only=True)
    records = []

    for sheet_name in wb.sheetnames:
        if not WEEK_SHEET_RE.match(sheet_name):
            continue  # пропускаем служебные листы

        sheet = wb[sheet_name]

        # заголовки групп берём из строки 7
        group_names = {}
        for col in GROUP_COLS:
            val = sheet[f"{col}7"].value
            if val:
                group_names[col] = str(val).strip()

        row = FIRST_DAY_ROW
        for _ in range(DAYS_PER_WEEK):
            day_name = sheet[f"A{row}"].value
            date_val = sheet[f"B{row}"].value
            if not day_name:
                row += SLOTS_PER_DAY
                continue

            for slot in range(SLOTS_PER_DAY):
                r = row + slot
                time_val = sheet[f"C{r}"].value
                if not time_val:
                    continue

                for col, group in group_names.items():
                    cell_val = sheet[f"{col}{r}"].value
                    if cell_val:
                        records.append({
                            "week_sheet": sheet_name,
                            "day": str(day_name).strip(),
                            "date": date_val,
                            "time": str(time_val).strip(),
                            "group": group,
                            "subject": str(cell_val).strip(),
                        })

            row += SLOTS_PER_DAY

    return records


if __name__ == "__main__":
    data = parse_schedule("data/schedule.xlsx")
    print(f"Всего записей: {len(data)}")
    for r in data[:10]:
        print(r)