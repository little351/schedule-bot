"""Отметка посещаемости на сайте вуза (модуль Moodle «Посещаемость»), только для владельца бота.

Как устроено на сайте: у курса есть страница посещаемости mod/attendance/view.php?id=…; пока преподаватель
держит занятие открытым для самоотметки, на ней есть ссылка mod/attendance/attendance.php?sessid=…&sesskey=…
Ссылка ведёт на форму: выбрать статус («Присутствовал»), иногда ввести пароль от преподавателя, сохранить.

Бот делает то же, что владелец сделал бы в браузере, и только по его команде и подтверждению:
/mark находит страницу с открытой отметкой, спрашивает «Да / Нет», после «Да» отправляет форму и проверяет,
что ссылка на отметку исчезла (значит, отметка принята). Логин и пароль - те же LMS_LOGIN и LMS_PASSWORD.
"""
import html
import re
from urllib.parse import urljoin

from lms_sync import SyncError, _text

VIEW_ID_RE = re.compile(r"mod/attendance/view\.php\?(?:[^#\s]*&)?id=(\d+)")
MARK_HREF_RE = re.compile(r'href=["\']([^"\']*mod/attendance/attendance\.php\?[^"\']*)["\']', re.IGNORECASE)
SESSID_RE = re.compile(r"[?&]sessid=(\d+)")
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.DOTALL | re.IGNORECASE)
H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.DOTALL | re.IGNORECASE)
FORM_RE = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.DOTALL | re.IGNORECASE)
INPUT_RE = re.compile(r"<input\b([^>]*)/?>", re.IGNORECASE)
ATTR_RE = re.compile(r'([\w:-]+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\')')
LABEL_FOR_RE = re.compile(r'<label\b[^>]*\bfor=["\']([^"\']+)["\'][^>]*>(.*?)</label>', re.DOTALL | re.IGNORECASE)
LABEL_WRAP_RE = re.compile(r"<label\b[^>]*>(.*?)</label>", re.DOTALL | re.IGNORECASE)
ALERT_RE = re.compile(r'<div[^>]*class=["\'][^"\']*(?:alert|notifyproblem|error|errorbox)[^"\']*["\'][^>]*>(.*?)</div>',
                      re.DOTALL | re.IGNORECASE)
PRESENT_WORDS = ("присутств", "present")
SKIP_BUTTONS = ("cancel",)


def page_path(url_or_id):
    """Ссылка на страницу посещаемости (или её номер) -> /mod/attendance/view.php?id=N; не она -> None."""
    text = str(url_or_id).strip()
    if text.isdigit():
        return f"/mod/attendance/view.php?id={text}"
    m = VIEW_ID_RE.search(html.unescape(text))
    return f"/mod/attendance/view.php?id={m.group(1)}" if m else None


def course_title(page_html):
    """Название страницы: «Курс: Посещаемость» из <title>, иначе заголовок."""
    m = TITLE_RE.search(page_html) or H1_RE.search(page_html)
    return _text(m.group(1)) if m else ""


def open_marks(page_html, page_url):
    """Ссылки на открытые сейчас отметки: [(sessid, полная ссылка)] без повторов."""
    found, seen = [], set()
    for href in MARK_HREF_RE.findall(page_html):
        href = html.unescape(href)
        m = SESSID_RE.search(href)
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            found.append((m.group(1), urljoin(page_url, href)))
    return found


def _attrs(fragment):
    return {k.lower(): html.unescape(v1 or v2) for k, v1, v2 in ATTR_RE.findall(fragment)}


def parse_form(page_html, page_url):
    """Форма отметки: {"action", "fields" (скрытые поля и кнопка сохранения), "statuses" [(value, подпись)],
    "password" (имя поля пароля или None)}. Нет формы с выбором статуса -> None."""
    for form_attrs, body in FORM_RE.findall(page_html):
        attrs = _attrs(form_attrs)
        if "attendance.php" not in attrs.get("action", "") and 'name="status"' not in body and "name='status'" not in body:
            continue
        labels = {for_id: _text(text) for for_id, text in LABEL_FOR_RE.findall(body)}
        fields, statuses, password = {}, [], None
        for raw in INPUT_RE.findall(body):
            a = _attrs(raw)
            kind, name = a.get("type", "text").lower(), a.get("name")
            if not name:
                continue
            if kind == "radio" and name == "status":
                statuses.append((a.get("value", ""), labels.get(a.get("id", ""), "")))
            elif kind == "password":
                password = name
            elif kind == "hidden":
                fields[name] = a.get("value", "")
            elif kind == "submit" and name not in SKIP_BUTTONS and name not in fields:
                fields[name] = a.get("value", "")
        if not statuses:
            continue
        if any(not label for _, label in statuses):
            # подпись вокруг кнопки: <label><input type="radio" ...> Присутствовал</label>
            wrapped = {}
            for lbl in LABEL_WRAP_RE.findall(body):
                m = INPUT_RE.search(lbl)
                if m and _attrs(m.group(1)).get("name") == "status":
                    wrapped[_attrs(m.group(1)).get("value", "")] = _text(lbl)
            statuses = [(v, t or wrapped.get(v, "")) for v, t in statuses]
        return {"action": urljoin(page_url, attrs.get("action") or page_url), "fields": fields,
                "statuses": statuses, "password": password}
    return None


def present_status(statuses):
    """Статус «Присутствовал» (по подписи); не нашли - None, тогда бот ничего не отправляет."""
    for value, label in statuses:
        if any(word in label.lower() for word in PRESENT_WORDS):
            return value, label
    return None


def page_problem(page_html):
    """Текст предупреждения Moodle на странице (неверный пароль, отметка закрыта и т.п.), если есть."""
    for fragment in ALERT_RE.findall(page_html):
        text = _text(fragment)
        if text:
            return text[:300]
    return ""


def find_open(client, pages):
    """Открытые отметки на страницах владельца: [{"page", "title", "sessid"}] и список недоступных страниц."""
    found, problems = [], []
    for path in pages:
        try:
            text, url = client.page(path)
        except SyncError as e:
            problems.append(f"{path}: {e}")
            continue
        title = course_title(text)
        for sessid, _ in open_marks(text, url):
            found.append({"page": path, "title": title, "sessid": sessid})
    return found, problems


def _mark_link(client, page, sessid):
    text, url = client.page(page)
    for sid, link in open_marks(text, url):
        if sid == str(sessid):
            return link
    return None


def mark(client, page, sessid, password=None):
    """Отметиться на занятии sessid страницы page. Возвращает ("ok", подпись статуса), ("password", "")
    если нужен пароль от преподавателя, или ("error", причина)."""
    link = _mark_link(client, page, sessid)
    if not link:
        return "error", "отметка на этом занятии уже закрыта или уже сделана"
    text, url = client.page(link)
    form = parse_form(text, url)
    if not form:
        return "error", page_problem(text) or "на странице отметки нет формы с выбором статуса"
    status = present_status(form["statuses"])
    if not status:
        labels = ", ".join(label or value for value, label in form["statuses"])
        return "error", f"в форме нет статуса «Присутствовал» (есть: {labels})"
    if form["password"] and not password:
        return "password", ""
    data = dict(form["fields"])
    data["status"] = status[0]
    if form["password"]:
        data[form["password"]] = password
    response = client._request("POST", form["action"], data=data)
    if response.status_code >= 400:
        return "error", f"сайт ответил кодом {response.status_code}"
    problem = page_problem(response.text) if "mod/attendance/attendance.php" in str(response.url) else ""
    # проверка: ссылка на отметку этого занятия должна исчезнуть со страницы посещаемости
    if _mark_link(client, page, sessid) is None:
        return "ok", status[1]
    return "error", problem or "сайт не принял отметку: ссылка на неё всё ещё есть"
