"""Телефоны и почты преподавателей с сайта вуза (gtsolifk.ru) -> data/teachers.json.

Запуск (из корня репозитория, нужен доступ к gtsolifk.ru):
    python scripts/teacher_contacts.py            # обновить data/teachers.json
    python scripts/teacher_contacts.py --dry-run  # только показать, что нашлось
    python scripts/teacher_contacts.py --dump URL # показать текст страницы, как его видит скрипт
    python scripts/teacher_contacts.py --from-log FILE  # применить страницы из лога прошлого запуска (строка PAGES_JSON)

Как работает: берёт список кафедр со страницы /obrazovanie/kafedri, на каждой странице кафедры
ищет карточки сотрудников (ФИО, затем телефон «+7 (495) 961-31-11, доб. NN-NN» и почта @gtsolifk.ru)
и сопоставляет ФИО с нашим списком (без учёта регистра и ё/е; внутри своей кафедры прощает опечатки
вроде Наталия/Наталья). Ссылка на страницу кафедры пишется в её поле source. Берутся только рабочие контакты,
опубликованные на сайте. Если у человека на сайте контактов нет, его запись не меняется;
если контакт с сайта пропал, он удаляется из файла (сайт - единственный источник).
"""
import argparse
import difflib
import html
import json
import os
import re
import sys
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

BASE = "https://gtsolifk.ru"
INDEX = BASE + "/obrazovanie/kafedri"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEACHERS_FILE = os.path.join(ROOT, "data", "teachers.json")

PHONE_RE = re.compile(r"\+7\s*\(\d{3}\)\s*[\d-]{7,9}(?:\s*,?\s*доб\.?\s*[\d-]+)?", re.I)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
UNI_EMAIL_RE = re.compile(r"(?<![\w.%+@-])[A-Za-z0-9._%+-]+@gtsolifk\.ru(?![\w-]|\.\w)", re.I)
CARD_LINES = 40  # дальше этого от ФИО контакты не ищем (подвал страницы)
# ФИО полностью: три слова с заглавной кириллической буквы (фамилия может быть двойной)
FIO_RE = re.compile(r"[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+")


def norm(name):
    return " ".join(name.replace("ё", "е").replace("Ё", "Е").lower().split())


class TextParser(HTMLParser):
    """Текст страницы построчно; mailto:/tel: из ссылок тоже попадают в текст."""
    BLOCK = {"p", "div", "br", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "a", "span"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.skip += 1
        a = dict(attrs)
        if tag == "a" and a.get("href"):
            href = a["href"]
            self.links.append(href)
            if href.lower().startswith("mailto:"):
                self.parts.append("\n" + html.unescape(href[7:].split("?")[0]) + "\n")
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self.skip:
            self.skip -= 1
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)

    def lines(self):
        text = "".join(self.parts).replace("\xa0", " ")
        return [ln.strip() for ln in text.split("\n") if ln.strip()]


def parse(page_html):
    p = TextParser()
    p.feed(page_html)
    return p.lines(), p.links


def department_urls(client):
    _, links = parse(client.get(INDEX).text)
    urls = []
    for href in links:
        url = urljoin(INDEX + "/", href)
        path = urlparse(url).path.rstrip("/")
        if urlparse(url).netloc.endswith("gtsolifk.ru") and path.startswith("/obrazovanie/kafedri/") and path.count("/") == 3:
            url = BASE + path
            if url not in urls:
                urls.append(url)
    return urls


def extract_contacts(lines):
    """[(ФИО, телефон, почта)]: контакт относится к последнему полному ФИО перед ним.

    Карточка на сайте: ФИО, должность, телефон, почта. Почта закрывает карточку, а дальше
    CARD_LINES строк от ФИО контакты не ищем: так подвал страницы не достаётся последнему в списке."""
    found = {}
    order = []
    current = None
    since = 0
    for ln in lines:
        fio = FIO_RE.fullmatch(ln) or (len(ln) < 60 and FIO_RE.match(ln))
        if fio:
            current = fio.group(0)
            since = 0
            continue
        since += 1
        if not current or since > CARD_LINES:
            continue
        rec = found.setdefault(current, {"phone": None, "email": None})
        if current not in order:
            order.append(current)
        phone = PHONE_RE.search(ln)
        if phone and not rec["phone"]:
            rec["phone"] = clean_phone(phone.group(0))
        if not rec["email"]:
            rec["email"] = find_email(ln)
            if rec["email"]:
                current = None
    return [(n, found[n]["phone"], found[n]["email"]) for n in order if found[n]["phone"] or found[n]["email"]]


def find_email(line):
    """Почта вуза целиком; адрес с опечаткой (кириллица вплотную и т.п.) не берём, чтобы не записать обрывок."""
    for m in UNI_EMAIL_RE.finditer(line):
        email = m.group(0).lower()
        if email != "rectorat@gtsolifk.ru":  # подвал сайта (приёмная ректора)
            return email
    return None


def clean_phone(s):
    s = " ".join(s.split())
    m = re.match(r"(\+7\s*\(\d{3}\)\s*[\d-]+)\s*,?\s*(?:доб\.?\s*([\d-]+))?", s, re.I)
    if not m:
        return s
    return m.group(1) + (", доб. " + m.group(2) if m.group(2) else "")


def page_title(lines):
    """Название кафедры из заголовка страницы («Кафедра ... · РУС «ГЦОЛИФК»»)."""
    for ln in lines:
        if ln.lower().startswith("кафедра"):
            return ln.split(" · ")[0].strip()
    return None


def similar(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio()


def fio_close(site, ours):
    """Одно и то же лицо с опечаткой: фамилия и имя почти совпадают (Наталия/Наталья, Тавыриков/Тавриков),
    отчество может отличаться или отсутствовать. Используется только внутри одной кафедры."""
    a, b = norm(site).replace("ия", "ья").split(), norm(ours).replace("ия", "ья").split()
    if len(a) < 2 or len(b) < 2:
        return False
    return similar(a[0], b[0]) >= 0.85 and similar(a[1], b[1]) >= 0.8


def match(pages, departments):
    """Сопоставить карточки с сайта с нашим списком. -> {id преподавателя: (телефон, почта)}, источники, отчёт."""
    by_name = {}
    for dept in departments:
        for t in dept["teachers"]:
            by_name.setdefault(norm(t["name"]), []).append(t)
    contacts, sources, fuzzy, unmatched = {}, {}, [], []

    def put(t, phone, email):
        old = contacts.get(t["id"], (None, None))
        contacts[t["id"]] = (old[0] or phone, old[1] or email)

    for page in pages:
        dept = None
        if page["title"]:
            best = max(departments, key=lambda d: similar(norm(d["name"]), norm(page["title"])))
            if similar(norm(best["name"]), norm(page["title"])) >= 0.75:
                dept = best
                sources[dept["id"]] = page["url"]
        for name, phone, email in page["contacts"]:
            exact = by_name.get(norm(name))
            if exact:
                for t in exact:
                    put(t, phone, email)
                continue
            cands = [t for t in (dept["teachers"] if dept else []) if fio_close(name, t["name"])]
            if len(cands) == 1:
                put(cands[0], phone, email)
                fuzzy.append((name, cands[0]["name"]))
            elif email != "rectorat@gtsolifk.ru":  # подвал сайта (приёмная ректора), не карточка кафедры
                unmatched.append((name, phone, email, page["url"]))
    return contacts, sources, fuzzy, unmatched


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--dump")
    ap.add_argument("--file", default=TEACHERS_FILE)
    ap.add_argument("--from-log", help="не ходить на сайт, взять страницы из строки PAGES_JSON прошлого запуска")
    args = ap.parse_args()

    if args.dump:
        lines, _ = parse(fetch(args.dump))
        print("\n".join(lines))
        return 0

    with open(args.file, encoding="utf-8") as f:
        data = json.load(f)
    departments = data["departments"]

    if args.from_log:
        with open(args.from_log, encoding="utf-8") as f:
            line = next(ln for ln in f if ln.startswith("PAGES_JSON "))
        pages = json.loads(line.split(" ", 1)[1])
    else:
        pages = []
        with httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": "schedule-bot teacher contacts"}) as client:
            urls = department_urls(client)
            print(f"Страниц кафедр: {len(urls)}")
            for url in urls:
                try:
                    resp = client.get(url)
                    resp.raise_for_status()
                except httpx.HTTPError as e:
                    print(f"! {url}: {e}")
                    continue
                lines, _ = parse(resp.text)
                pages.append({"url": url, "title": page_title(lines), "contacts": extract_contacts(lines)})
        if not pages:
            print("Ни одной страницы не загрузилось, файл не меняю")
            return 1

    contacts, sources, fuzzy, unmatched = match(pages, departments)

    changed = 0
    ids = set()
    for dept in departments:
        if dept["id"] in sources:
            dept["source"] = sources[dept["id"]]
        for t in dept["teachers"]:
            ids.add(t["id"])
            phone, email = contacts.get(t["id"], (None, None))
            before = (t.get("phone"), t.get("email"))
            for field, value in (("phone", phone), ("email", email)):
                if value:
                    t[field] = value
                else:
                    t.pop(field, None)
            changed += before != (t.get("phone"), t.get("email"))

    for page in pages:
        print(f"{len(page['contacts']):3} карточек  {page['title']}  {page['url']}")
    print(f"\nКафедр с привязанной страницей: {len(sources)} из {len(departments)}")
    print(f"Людей в списке: {len(ids)}, с контактами: {len(contacts)}, изменено записей: {changed}")
    print(f"Телефон: {sum(1 for c in contacts.values() if c[0])}, почта: {sum(1 for c in contacts.values() if c[1])}")
    print(f"Без контактов: {', '.join(sorted({t['name'] for d in departments for t in d['teachers'] if t['id'] not in contacts}))}")
    if fuzzy:
        print(f"\nСопоставлено с поправкой на опечатку ({len(fuzzy)}): сайт -> наш список")
        for site, ours in fuzzy:
            print(f"  {site} -> {ours}")
    if unmatched:
        print(f"\nНа сайте есть, в нашем списке нет ({len(unmatched)}):")
        for name, phone, email, url in unmatched:
            print(f"  {name} | {phone} | {email} | {url.rsplit('/', 1)[-1]}")
    if not args.from_log:
        print("\nPAGES_JSON " + json.dumps(pages, ensure_ascii=False))

    if not args.dry_run:
        with open(args.file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.write("\n")
    return 0


def fetch(url):
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        return client.get(url).text


if __name__ == "__main__":
    sys.exit(main())
