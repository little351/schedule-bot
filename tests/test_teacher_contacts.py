import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import teacher_contacts as tc  # noqa: E402

PAGE = """<html><head><title>Кафедра педагогики · РУС «ГЦОЛИФК»</title></head><body>
<nav><a href="/obrazovanie/kafedri/pedagogiki">Кафедра педагогики</a><div>Боев Максим Юрьевич - ректор</div>
<a href="/obrazovanie/kafedri/pedagogiki/stav_01">Кадровый состав</a><a href="/obrazovanie/kafedri/fiziologii">Другая</a></nav>
<h1>Кафедра педагогики</h1>
<p>Профессор Неверкович Сергей Дмитриевич много лет руководил кафедрой.</p>
<div class="card"><div>Левченкова Татьяна Викторовна</div><div>заведующий кафедрой</div>
 <div class="modal"><div>Левченкова Татьяна Викторовна</div><div>заведующий кафедрой</div>
  <div>Должность</div><div>Заведующий кафедрой</div>
  <div>Регалии</div><div>Доктор педагогических наук</div><div>Профессор</div>
  <div>Адрес</div><div>Медико-биологический корпус, каб. 401</div>
  <div>Телефон</div><div>+7 (495) 961-31-11, доб.&nbsp;54-64</div>
  <div>Электронная почта</div><a href="mailto:levchenkova.tv@gtsolifk.ru">levchenkova.tv@gtsolifk.ru</a>
  <p>Образование: Московский областной государственный институт физической культуры</p>
  <p>Индекс Хирша — 10</p>
 </div></div>
<div class="card"><div>Германов Геннадий Николаевич</div><div>профессор</div><div>Телефон</div><div>+7 (495) 961-31-11</div></div>
<div class="card"><div>Киселёва Елена Владимировна</div><div>доцент</div><div>kiselevа.ev@gtsolifk.ru</div></div>
<div class="card"><div>Аронова Наталия Владимировна</div><div>доцент</div><div>aronova@gtsolifk.ru</div></div>
<footer><div>+7 (495) 961-31-11</div><div>rectorat@gtsolifk.ru</div><div>Адрес</div><div>Сиреневый бульвар, 4</div></footer>
</body></html>"""
URL = "https://gtsolifk.ru/obrazovanie/kafedri/pedagogiki"


def cards_by_name(page=PAGE):
    lines, _ = tc.parse(page)
    return {c["name"]: c for c in tc.extract_cards(lines)}


def test_extract_cards():
    lines, links = tc.parse(PAGE)
    assert tc.page_title(lines) == "Кафедра педагогики"
    assert tc.subpage_urls(URL, links) == [URL + "/stav_01"]
    got = cards_by_name()
    lev = got["Левченкова Татьяна Викторовна"]
    assert lev["phone"] == "+7 (495) 961-31-11, доб. 54-64"
    assert lev["email"] == "levchenkova.tv@gtsolifk.ru"
    assert lev["address"] == "Медико-биологический корпус, каб. 401"
    assert lev["regalia"] == ["Доктор педагогических наук", "Профессор"]
    assert lev["info"] == ["Образование: Московский областной государственный институт физической культуры",
                           "Индекс Хирша — 10"]
    assert got["Германов Геннадий Николаевич"]["phone"] == "+7 (495) 961-31-11"
    assert got["Германов Геннадий Николаевич"]["email"] is None
    # адрес с кириллической буквой внутри - опечатка сайта, обрывок не записываем
    assert "Киселёва Елена Владимировна" not in got
    # упоминание в тексте без контактов карточкой не считается
    assert "Неверкович Сергей Дмитриевич" not in got
    # подвал страницы не достаётся последнему в списке
    aronova = got["Аронова Наталия Владимировна"]
    assert (aronova["phone"], aronova["email"], aronova["address"]) == (None, "aronova@gtsolifk.ru", None)


def test_match_and_apply():
    lines, _ = tc.parse(PAGE)
    pages = [{"url": URL, "title": tc.page_title(lines), "cards": tc.extract_cards(lines)}]
    departments = [
        {"id": "pedagogiki", "name": "Кафедра педагогики", "teachers": [
            {"id": "a", "name": "Левченкова Татьяна Викторовна", "position": "x", "phone": "старый"},
            {"id": "b", "name": "Германов Геннадий Николаевич", "position": "x", "email": "old@gtsolifk.ru"},
            {"id": "c", "name": "Аронова Наталья Владимировна", "position": "x"},
        ]},
        {"id": "other", "name": "Кафедра футбола", "teachers": [
            {"id": "d", "name": "Аронова Наталья Петровна", "position": "x"},
        ]},
    ]
    found, sources, fuzzy, unmatched = tc.match(pages, departments)
    assert sources == {"pedagogiki": URL}
    assert fuzzy == [("Аронова Наталия Владимировна", "Аронова Наталья Владимировна")]
    assert "d" not in found  # опечатки прощаются только внутри своей кафедры
    changed, renamed = tc.apply(departments, found, sources)
    a, b, c = departments[0]["teachers"]
    assert departments[0]["source"] == URL and "source" not in departments[1]
    assert a["phone"] == "+7 (495) 961-31-11, доб. 54-64" and a["regalia"] == ["Доктор педагогических наук", "Профессор"]
    assert "email" not in b  # контакт пропал с сайта - убираем
    assert c["id"] == "c" and c["name"] == "Аронова Наталия Владимировна"  # ФИО по сайту, id прежний
    assert renamed == [("Аронова Наталья Владимировна", "Аронова Наталия Владимировна")]
    assert changed == 3


def test_site_name_keeps_our_clarification():
    assert tc.site_name("Мирзоев Октай Мирза оглы (Октай Мирзоевич)", "Мирзоев Октай Мирза оглы") == \
        "Мирзоев Октай Мирза оглы (Октай Мирзоевич)"
    assert tc.site_name("Тавриков Евгений Евгеньевич", "Тавыриков Евгений Евгеньевич") == "Тавыриков Евгений Евгеньевич"


def test_teachers_file_contacts_are_well_formed():
    with open(os.path.join(ROOT, "data", "teachers.json"), encoding="utf-8") as f:
        data = json.load(f)
    for dept in data["departments"]:
        for t in dept["teachers"]:
            if "phone" in t:
                assert tc.PHONE_RE.fullmatch(t["phone"]), t
            if "email" in t:
                assert tc.EMAIL_RE.fullmatch(t["email"]) and t["email"].endswith("@gtsolifk.ru"), t
            for key in ("regalia", "info"):
                assert all(isinstance(x, str) and x for x in t.get(key, [])), t
