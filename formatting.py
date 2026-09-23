import re
from database import DAYS_ORDER, TIME_SLOTS

MDV2_SPECIAL = r'_*[]()~`>#+-=|{}.!'


def escape_md(text):
    return re.sub(f"([{re.escape(MDV2_SPECIAL)}])", r"\\\1", str(text))


def trim_trailing_empty(slots):
    last_idx = -1
    for i, subj in enumerate(slots):
        if subj:
            last_idx = i
    if last_idx == -1:
        return None
    return slots[:last_idx + 1]

def format_week_text(group_name, week_sheet, grid):
    lines = [f"*Неделя {escape_md(week_sheet)}*", f"*Расписание {escape_md(group_name)}*", ""]
    for day in DAYS_ORDER:
        subjects = [grid[day][t] for t in TIME_SLOTS]
        trimmed = trim_trailing_empty(subjects)
        if trimmed is None:
            continue
        lines.append(f"*{day}:*")
        for t, subj in zip(TIME_SLOTS, trimmed):
            display_time = escape_md(t.replace(".", ":"))
            if subj:
                lines.append(f"*{display_time}* — {escape_md(subj)}")
            else:
                lines.append(f"*{display_time}* —")
        lines.append("")
    return "\n".join(lines).strip()


def format_day_text(group_name, week_sheet, day_name, grid):
    if day_name not in grid:
        return f"На {day_name.lower()} пар нет \\(выходной\\)\\."

    subjects = [grid[day_name][t] for t in TIME_SLOTS]
    trimmed = trim_trailing_empty(subjects)
    if trimmed is None:
        return f"На {escape_md(day_name.lower())} пар нет\\."

    lines = [f"*{day_name}*, неделя {escape_md(week_sheet)}", f"*Расписание {escape_md(group_name)}*", ""]
    for t, subj in zip(TIME_SLOTS, trimmed):
        display_time = escape_md(t.replace(".", ":"))
        if subj:
            lines.append(f"*{display_time}* — {escape_md(subj)}")
        else:
            lines.append(f"*{display_time}* —")
    return "\n".join(lines)