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
import time
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


PAUSE = 2  # секунд между запросами


def get(client, url, attempts=3):
    """Сайт вуза иногда долго не отвечает: несколько попыток с паузой."""
    for i in range(attempts):
        time.sleep(PAUSE)  # не нагружать сайт вуза: после частых запросов он перестаёт отвечать
        try:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.text
        except httpx.HTTPError:
            if i == attempts - 1:
                raise
            time.sleep(20 * (i + 1))


STAFF_PAGE_RE = re.compile(r"stav|sostav|person|sotrudn", re.I)  # адрес страницы «Кадровый состав» (…/stav_01770)


def subpage_urls(dept_url, links, limit=3):
    """Подстраницы кафедры с кадровым составом: на один уровень глубже её адреса, в адресе «stav» и т.п."""
    base = urlparse(dept_url).path.rstrip("/")
    urls = []
    for href in links:
        url = urljoin(dept_url + "/", href)
        path = urlparse(url).path.rstrip("/")
        last = path.rsplit("/", 1)[-1]
        if urlparse(url).netloc.endswith("gtsolifk.ru") and path.startswith(base + "/") \
                and path.count("/") == base.count("/") + 1 and STAFF_PAGE_RE.search(last):
            url = BASE + path
            if url not in urls:
                urls.append(url)
    return urls[:limit]


def department_urls(client):
    _, links = parse(get(client, INDEX))
    urls = []
    for href in links:
        url = urljoin(INDEX + "/", href)
        path = urlparse(url).path.rstrip("/")
        if urlparse(url).netloc.endswith("gtsolifk.ru") and path.startswith("/obrazovanie/kafedri/") and path.count("/") == 3:
            url = BASE + path
            if url not in urls:
                urls.append(url)
    return urls


# подписи полей в карточке сотрудника на сайте
LABELS = {
    "должность": "site_position",
    "регалии": "regalia",
    "адрес": "address",
    "телефон": "phone",
    "электронная почта": "email",
    "e-mail": "email",
    "почта": "email",
}
# строки после контактов, которые стоит показать студенту («Образование: ...» и т.п.)
INFO_RE = re.compile(r"^(Образование|Уровень образования|Сфера научных интересов|Область научных интересов|Научные интересы|Индекс Хирша)\b", re.I)
NAME_LINE_RE = re.compile(r"[А-ЯЁ][А-Яа-яЁё() -]+")  # ФИО целиком, вкл. «оглы», «(Октай Мирзоевич)»


PATRONYMIC_RE = re.compile(r"(вич|вна|ична|чна|оглы|кызы|улы)$")
STOP_LINES = {"×", "✕", "Новости и события"}  # конец всплывающей карточки / начало ленты новостей
MAX_INFO_LINES = 12


def card_name(line):
    """ФИО из строки карточки: «Иванов Иван Иванович» или «Иванов Иван Иванович - доцент»."""
    parts = re.split(r"\s+[-—–]\s*", line, maxsplit=1)  # «ФИО - доцент», «ФИО -доцент»
    part = parts[0].strip()
    if not (len(part) < 80 and NAME_LINE_RE.fullmatch(part) and FIO_RE.match(part)):
        return None
    words = part.split()
    # «Финалист Чемпионата России среди ...» - не ФИО: нужно отчество, подпись «- должность» или ровно три слова
    if any(PATRONYMIC_RE.search(w) for w in words[2:]) or len(parts) > 1 or len(words) == 3:
        return " ".join(words)
    return None


def join_info(raw):
    """Строки описания -> пункты: «Образование: ...» начинает пункт, продолжение (со строчной буквы
    или после «:», «,», «;») приклеивается к предыдущему."""
    items = []
    for ln in raw:
        cont = items and (ln[:1].islower() or items[-1].endswith((":", ",", ";")))
        if cont and not INFO_RE.match(ln):
            items[-1] = (items[-1] + " " + ln)[:600]
        elif ln not in items:
            items.append(ln[:600])
    return items


def extract_cards(lines):
    """Карточки сотрудников: [{name, phone, email, address, regalia, info}].

    Карточка на сайте: ФИО (иногда «ФИО - должность»), должность, (ФИО ещё раз во всплывающем окне),
    «Должность», «Регалии», «Адрес», «Телефон», «Электронная почта», затем описание
    («Образование: ...», интересы, индекс Хирша) до «×». Описание бывает и до подписей полей.
    Телефон и почта ищутся не дальше CARD_LINES строк от ФИО; почта закрывает поиск контактов,
    так подвал страницы не достаётся последнему в списке."""
    cards = []
    card = None
    section = None
    since = 0
    closed = False      # почта найдена: дальше только описание
    info_open = False   # собираем описание после почты
    labeled = False     # уже были подписи полей («Должность», «Телефон»...)
    for ln in lines:
        name = card_name(ln)
        if name:
            if card is None or norm(card["name"]) != norm(name):
                card = {"name": name, "phone": None, "email": None, "address": None, "regalia": [], "raw": []}
                cards.append(card)
                closed = info_open = labeled = False
            section, since = None, 0
            continue
        since += 1
        if card is None or since > CARD_LINES:
            continue
        if ln in STOP_LINES:
            section, info_open = None, False
            continue
        label = LABELS.get(ln.lower().rstrip(":").strip())
        if label:
            labeled = True
            section = None if closed else label  # после почты карточка кончилась
            info_open = False if closed else info_open
            continue
        if closed:
            if PHONE_RE.search(ln) or find_email(ln):
                continue  # повтор контактов (во всплывающем окне они дублируются)
            looks_info = INFO_RE.match(ln) or ":" in ln or ln[:1].islower() or \
                (card["raw"] and card["raw"][-1].endswith((":", ",", ";")))
            if not looks_info:
                info_open = False  # дальше уже не описание (меню, подвал)
            if info_open and len(card["raw"]) < MAX_INFO_LINES:
                card["raw"].append(ln)
            continue
        phone = PHONE_RE.search(ln)
        if phone and not card["phone"]:
            card["phone"] = clean_phone(phone.group(0))
        email = find_email(ln)
        if email and not card["email"]:
            card["email"] = email
            closed = info_open = True
        if phone or email:
            section = None
            continue
        if section == "regalia" and len(ln) <= 120 and len(card["regalia"]) < 6 and ln not in card["regalia"]:
            card["regalia"].append(ln)
        elif section == "address" and not card["address"] and len(ln) <= 150 and re.search(r"\w\w", ln):
            card["address"] = ln
        elif not labeled and section is None and (ln[:1].isupper() or INFO_RE.match(ln)) \
                and len(card["raw"]) < MAX_INFO_LINES:
            card["raw"].append(ln)  # описание перед подписями полей (формат «ФИО - должность»)
    result = []
    for c in cards:
        raw = c.pop("raw")
        c["info"] = join_info(raw) if c["phone"] or c["email"] else []
        if c["phone"] or c["email"] or c["address"] or c["regalia"]:
            result.append(c)
    return result


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
    sur, first = similar(a[0], b[0]), similar(a[1], b[1])  # Пазына/Пазина 0.83, Асыл/Асыя 0.75
    return sur >= 0.75 and first >= 0.75 and sur + first >= 1.7


FIELDS = ("phone", "email", "address", "regalia", "info")


def match(pages, departments):
    """Сопоставить карточки с сайта с нашим списком.

    -> ({id преподавателя: карточка}, {id кафедры: url}, [(сайт, наш)], [(ФИО, url)])"""
    by_name = {}
    for dept in departments:
        for t in dept["teachers"]:
            by_name.setdefault(norm(t["name"]), []).append(t)
    found, sources, fuzzy, unmatched = {}, {}, [], []

    def put(t, card):
        old = found.setdefault(t["id"], {"name": card["name"]})
        for f in FIELDS:
            if not old.get(f) and card.get(f):
                old[f] = card[f]

    for page in pages:
        dept = None
        if page["title"]:
            best = max(departments, key=lambda d: similar(norm(d["name"]), norm(page["title"])))
            if similar(norm(best["name"]), norm(page["title"])) >= 0.75:
                dept = best
                sources.setdefault(dept["id"], page["url"])
        for card in page["cards"]:
            card["name"] = re.split(r"\s+[-—–]\s*", card["name"], maxsplit=1)[0].strip()
            exact = by_name.get(norm(card["name"]))
            if exact:
                for t in exact:
                    put(t, card)
                continue
            cands = [t for t in (dept["teachers"] if dept else []) if fio_close(card["name"], t["name"])]
            if len(cands) == 1:
                put(cands[0], card)
                if (card["name"], cands[0]["name"]) not in fuzzy:
                    fuzzy.append((card["name"], cands[0]["name"]))
            elif card.get("phone") or card.get("email"):
                unmatched.append((card["name"], page["url"]))
    return found, sources, fuzzy, unmatched


# опечатки на сайте, которым не верим: ФИО с сайта -> оставить наше написание
KEEP_OUR_NAME = {
    "Диванкувлиев Довлет Гаипович",  # у нас и в расписании «Диванкулиев»
}


def site_name(ours, site):
    """ФИО как на сайте; наше оставляем, только если оно и есть сайтовое с уточнением в конце
    («Мирзоев Октай Мирза оглы (Октай Мирзоевич)» при «Мирзоев Октай Мирза оглы» на сайте)."""
    if norm(ours).startswith(norm(site)) or site in KEEP_OUR_NAME:
        return ours
    return site


SKIP_INFO_RE = re.compile(r"^Дата рождения", re.I)  # личное, студентам не нужно


def clean_card(card):
    """Убрать заглушки («...» вместо кабинета) и личное (дата рождения); точка в конце регалий."""
    card = dict(card)
    if card.get("address") and not re.search(r"\w\w", card["address"]):
        card["address"] = None
    card["regalia"] = [r.rstrip(".").strip() for r in card.get("regalia") or [] if re.search(r"\w\w", r)]
    card["info"] = [i for i in card.get("info") or [] if not SKIP_INFO_RE.match(i)]
    return card


def apply(departments, found, sources):
    """Записать найденное в список. -> (изменено записей, [(было, стало)])."""
    changed, renamed = 0, []
    for dept in departments:
        if dept["id"] in sources:
            dept["source"] = sources[dept["id"]]
        for t in dept["teachers"]:
            card = clean_card(found[t["id"]]) if t["id"] in found else {}
            before = json.dumps(t, ensure_ascii=False, sort_keys=True)
            if card:
                new_name = site_name(t["name"], card["name"])
                if new_name != t["name"]:
                    renamed.append((t["name"], new_name))
                    t["name"] = new_name
            for f in FIELDS:
                if card.get(f):
                    t[f] = card[f]
                else:
                    t.pop(f, None)
            changed += before != json.dumps(t, ensure_ascii=False, sort_keys=True)
    return changed, renamed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--dump")
    ap.add_argument("--file", default=TEACHERS_FILE)
    ap.add_argument("--from-log", help="не ходить на сайт, взять страницы из строки PAGES_JSON прошлого запуска")
    args = ap.parse_args()

    if args.dump:
        lines, links = parse(fetch(args.dump))
        print("\n".join(lines))
        print("\nССЫЛКИ:\n" + "\n".join(l for l in links if "kafedri" in l))
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
        with httpx.Client(timeout=60, follow_redirects=True, headers={"User-Agent": "schedule-bot teacher contacts"}) as client:
            urls = department_urls(client)
            print(f"Страниц кафедр: {len(urls)}")
            for url in urls:
                try:
                    page_html = get(client, url)
                except httpx.HTTPError as e:
                    print(f"! {url}: {e!r}")
                    continue
                lines, links = parse(page_html)
                page = {"url": url, "title": page_title(lines), "cards": extract_cards(lines)}
                # «Кадровый состав» и т.п. на подстраницах кафедры
                for sub in subpage_urls(url, links):
                    try:
                        sub_lines, _ = parse(get(client, sub))
                    except httpx.HTTPError as e:
                        print(f"! {sub}: {e!r}")
                        continue
                    page["cards"] += extract_cards(sub_lines)
                pages.append(page)
        if not pages:
            print("Ни одной страницы не загрузилось, файл не меняю")
            return 1

    found, sources, fuzzy, unmatched = match(pages, departments)
    changed, renamed = apply(departments, found, sources)

    ids = {t["id"] for d in departments for t in d["teachers"]}
    with_contacts = {i for i, c in found.items() if c.get("phone") or c.get("email")}
    for page in pages:
        print(f"{len(page['cards']):3} карточек  {page['title']}  {page['url']}")
    print(f"\nКафедр с привязанной страницей: {len(sources)} из {len(departments)}")
    print(f"Людей в списке: {len(ids)}, с контактами: {len(with_contacts)}, изменено записей: {changed}")
    for f in FIELDS:
        print(f"  {f}: {sum(1 for c in found.values() if c.get(f))}")
    print(f"Без контактов: {', '.join(sorted({t['name'] for d in departments for t in d['teachers'] if t['id'] not in with_contacts}))}")
    if renamed:
        print(f"\nФИО исправлено по сайту ({len(renamed)}):")
        for old, new in renamed:
            print(f"  {old} -> {new}")
    if unmatched:
        print(f"\nНа сайте есть, в нашем списке нет ({len(unmatched)}):")
        for name, url in unmatched:
            print(f"  {name} | {url.rsplit('/', 1)[-1]}")
    if not args.from_log:
        print("\nPAGES_JSON " + json.dumps(pages, ensure_ascii=False))

    if not args.dry_run:
        with open(args.file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.write("\n")
    return 0


def fetch(url):
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        return get(client, url)


if __name__ == "__main__":
    sys.exit(main())
