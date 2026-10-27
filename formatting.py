import re
from database import DAYS_ORDER, PRACTICE_SLOT, TIME_SLOTS

# подпись под расписанием (то же в webapp/index.html); номер моноширинным: в Telegram он копируется нажатием
DONATION_NOTE = "Угости создателя чем-нибудь вкусным →"
DONATION_PHONE = "+79995567597"
DONATION_HINT = "(перевод по СБП)"

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
        practice = grid[day].get(PRACTICE_SLOT)
        if trimmed is None and not practice:
            continue
        lines.append(f"*{day}:*")
        for t, subj in zip(TIME_SLOTS, trimmed or []):
            display_time = escape_md(t.replace(".", ":"))
            if subj:
                lines.append(f"*{display_time}* — {escape_md(subj)}")
            else:
                lines.append(f"*{display_time}* —")
        if practice:
            lines.append(f"*{PRACTICE_SLOT}* — {escape_md(practice)}")
        lines.append("")
    return "\n".join(lines).strip()


def with_donation_note(text):
    """Текст расписания (MarkdownV2) с подписью через пустую строку."""
    return f"{text}\n\n{escape_md(DONATION_NOTE)} `{DONATION_PHONE}` {escape_md(DONATION_HINT)}"
