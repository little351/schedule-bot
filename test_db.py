from parser import parse_schedule
from database import init_db, save_schedule, get_week_schedule

data = parse_schedule("data/schedule.xlsx")
conn = init_db()
save_schedule(conn, data)

rows = get_week_schedule(conn, "13 группа", "14.09")
for r in rows:
    print(r)