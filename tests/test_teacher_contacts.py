import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import teacher_contacts as tc  # noqa: E402

PAGE = """<html><head><title>Кафедра педагогики · РУС «ГЦОЛИФК»</title></head><body>
<nav><a href="/obrazovanie/kafedri/pedagogiki">Кафедра педагогики</a><div>Боев Максим Юрьевич - ректор</div></nav>
<h1>Кафедра педагогики</h1>
<p>Профессор Неверкович Сергей Дмитриевич много лет руководил кафедрой.</p>
<div class="card"><div>Левченкова Татьяна Викторовна</div><div>заведующий кафедрой</div>
  <div>+7 (495) 961-31-11, доб.&nbsp;54-64</div><a href="mailto:levchenkova.tv@gtsolifk.ru">levchenkova.tv@gtsolifk.ru</a></div>
<div class="card"><div>Германов Геннадий Николаевич</div><div>профессор</div><div>+7 (495) 961-31-11</div></div>
<div class="card"><div>Киселёва Елена Владимировна</div><div>доцент</div><div>kiselevа.ev@gtsolifk.ru</div></div>
<div class="card"><div>Аронова Наталия Владимировна</div><div>доцент</div><div>aronova@gtsolifk.ru</div></div>
<footer><div>+7 (495) 961-31-11</div><div>rectorat@gtsolifk.ru</div></footer>
</body></html>"""


def test_extract_contacts_from_cards():
    lines, links = tc.parse(PAGE)
    assert tc.page_title(lines) == "Кафедра педагогики"
    got = {name: (phone, email) for name, phone, email in tc.extract_contacts(lines)}
    assert got["Левченкова Татьяна Викторовна"] == ("+7 (495) 961-31-11, доб. 54-64", "levchenkova.tv@gtsolifk.ru")
    assert got["Германов Геннадий Николаевич"] == ("+7 (495) 961-31-11", None)
    # адрес с кириллической буквой внутри - опечатка сайта, обрывок не записываем
    assert "Киселёва Елена Владимировна" not in got
    # упоминание в тексте без контактов карточкой не считается
    assert "Неверкович Сергей Дмитриевич" not in got


def test_match_exact_yo_and_typo_within_department():
    lines, _ = tc.parse(PAGE)
    pages = [{"url": "https://gtsolifk.ru/obrazovanie/kafedri/pedagogiki", "title": tc.page_title(lines),
              "contacts": tc.extract_contacts(lines)}]
    departments = [
        {"id": "pedagogiki", "name": "Кафедра педагогики", "teachers": [
            {"id": "a", "name": "Левченкова Татьяна Викторовна", "position": "x"},
            {"id": "b", "name": "Германов Геннадий Николаевич", "position": "x"},
            {"id": "c", "name": "Аронова Наталья Владимировна", "position": "x"},
        ]},
        {"id": "other", "name": "Кафедра футбола", "teachers": [
            {"id": "d", "name": "Аронова Наталья Петровна", "position": "x"},
        ]},
    ]
    contacts, sources, fuzzy, unmatched = tc.match(pages, departments)
    assert sources == {"pedagogiki": "https://gtsolifk.ru/obrazovanie/kafedri/pedagogiki"}
    assert contacts["a"] == ("+7 (495) 961-31-11, доб. 54-64", "levchenkova.tv@gtsolifk.ru")
    assert contacts["c"] == (None, "aronova@gtsolifk.ru")  # Наталия/Наталья, только своя кафедра
    assert "d" not in contacts
    assert fuzzy == [("Аронова Наталия Владимировна", "Аронова Наталья Владимировна")]
    assert all(u[2] != "rectorat@gtsolifk.ru" for u in unmatched)


def test_teachers_file_contacts_are_well_formed():
    with open(os.path.join(ROOT, "data", "teachers.json"), encoding="utf-8") as f:
        data = json.load(f)
    for dept in data["departments"]:
        for t in dept["teachers"]:
            if "phone" in t:
                assert tc.PHONE_RE.fullmatch(t["phone"]), t
            if "email" in t:
                assert tc.EMAIL_RE.fullmatch(t["email"]) and t["email"].endswith("@gtsolifk.ru"), t
