"""Телефоны и почты преподавателей с сайта вуза (gtsolifk.ru) -> data/teachers.json.

Запуск (из корня репозитория, нужен доступ к gtsolifk.ru):
    python scripts/teacher_contacts.py            # обновить data/teachers.json
    python scripts/teacher_contacts.py --dry-run  # только показать, что нашлось
    python scripts/teacher_contacts.py --dump URL # показать текст страницы, как его видит скрипт

Как работает: берёт список кафедр со страницы /obrazovanie/kafedri, на каждой странице кафедры
ищет карточки сотрудников (ФИО, затем телефон «+7 (495) 961-31-11, доб. NN-NN» и почта @gtsolifk.ru)
и сопоставляет ФИО с нашим списком (без учёта регистра и ё/е). Берутся только рабочие контакты,
опубликованные на сайте. Если у человека на сайте контактов нет, его запись не меняется;
если контакт с сайта пропал, он удаляется из файла (сайт - единственный источник).
"""
import argparse
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
    """[(ФИО, телефон, почта)]: контакт относится к последнему полному ФИО перед ним."""
    found = {}
    order = []
    current = None
    for ln in lines:
        fio = FIO_RE.fullmatch(ln) or (len(ln) < 60 and FIO_RE.match(ln))
        if fio:
            current = fio.group(0)
            continue
        if not current:
            continue
        rec = found.setdefault(current, {"phone": None, "email": None})
        if current not in order:
            order.append(current)
        phone = PHONE_RE.search(ln)
        if phone and not rec["phone"]:
            rec["phone"] = clean_phone(phone.group(0))
        email = EMAIL_RE.search(ln)
        if email and not rec["email"] and email.group(0).lower().endswith("@gtsolifk.ru"):
            rec["email"] = email.group(0).lower()
    return [(n, found[n]["phone"], found[n]["email"]) for n in order if found[n]["phone"] or found[n]["email"]]


def clean_phone(s):
    s = " ".join(s.split())
    m = re.match(r"(\+7\s*\(\d{3}\)\s*[\d-]+)\s*,?\s*(?:доб\.?\s*([\d-]+))?", s, re.I)
    if not m:
        return s
    return m.group(1) + (", доб. " + m.group(2) if m.group(2) else "")


def page_title(lines):
    for ln in lines:
        if ln.lower().startswith("кафедра "):
            return ln
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--dump")
    ap.add_argument("--file", default=TEACHERS_FILE)
    ap.add_argument("--from-json", help="не ходить на сайт, взять контакты из строки CONTACTS_JSON прошлого запуска")
    args = ap.parse_args()

    client = httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": "schedule-bot teacher contacts"})
    if args.dump:
        lines, _ = parse(client.get(args.dump).text)
        print("\n".join(lines))
        return 0

    with open(args.file, encoding="utf-8") as f:
        data = json.load(f)
    people = {}
    for dept in data["departments"]:
        for t in dept["teachers"]:
            people.setdefault(norm(t["name"]), []).append(t)

    contacts = {}       # норм. ФИО -> (phone, email)
    unmatched = []
    if args.from_json:
        with open(args.from_json, encoding="utf-8") as f:
            line = next(ln for ln in f if ln.startswith("CONTACTS_JSON "))
        contacts = {k: tuple(v) for k, v in json.loads(line.split(" ", 1)[1]).items()}
        urls = []
    else:
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
        recs = extract_contacts(lines)
        print(f"{len(recs):3} контактов  {url}")
        for name, phone, email in recs:
            key = norm(name)
            if key in people:
                old = contacts.get(key, (None, None))
                contacts[key] = (old[0] or phone, old[1] or email)
            else:
                unmatched.append((name, phone, email, url))

    total = sum(len(v) for v in people.values())
    changed = 0
    for key, records in people.items():
        phone, email = contacts.get(key, (None, None))
        for t in records:
            before = (t.get("phone"), t.get("email"))
            for field, value in (("phone", phone), ("email", email)):
                if value:
                    t[field] = value
                else:
                    t.pop(field, None)
            changed += before != (t.get("phone"), t.get("email"))

    with_any = sum(1 for k in people if k in contacts)
    print(f"\nВ списке {len(people)} человек ({total} записей), контакты найдены у {with_any}, изменено записей: {changed}")
    print(f"Телефон: {sum(1 for c in contacts.values() if c[0])}, почта: {sum(1 for c in contacts.values() if c[1])}")
    if unmatched:
        print(f"\nНа сайте есть, в нашем списке нет ({len(unmatched)}):")
        for name, phone, email, url in unmatched:
            print(f"  {name} | {phone} | {email} | {url.rsplit('/', 1)[-1]}")
    print("\nCONTACTS_JSON " + json.dumps({k: v for k, v in sorted(contacts.items())}, ensure_ascii=False))

    if not args.dry_run:
        with open(args.file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
