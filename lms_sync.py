"""Автоподгрузка файлов расписания с сайта вуза (Moodle).

Схема: войти по логину и паролю (форма входа Moodle с logintoken), найти файлы по названию
ресурса на страницах курса из data/lms_sources.json (id ссылок на файлы могут меняться, поэтому
жёсткие id не нужны), скачать, проверить парсером и сохранить в базу как последнюю хорошую версию.

Логин и пароль берутся только из переменных окружения LMS_LOGIN и LMS_PASSWORD. Они не пишутся
ни в лог, ни в отчёты: любой текст перед выводом проходит через scrub().

Порядок источников расписания: свежая подгрузка (эта база) > файлы, загруженные владельцем > файлы из репозитория.
"""
import asyncio
import hashlib
import html
import json
import logging
import os
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urljoin, urlparse

import httpx

from database import (
    delete_synced_file, get_sync_state, list_synced_files, save_synced_file, set_sync_state,
)
from schedule_parsers import parse_all, parse_file
from schedule_parsers.common import load_config

logger = logging.getLogger(__name__)

SOURCES_FILE = "lms_sources.json"
REQUEST_PAUSE = 1.0           # секунд между запросами к сайту: ходим вежливо, по одному
MAX_FILE_BYTES = 20 * 1024 * 1024
DEFAULT_MIN_RATIO = 0.6       # занятий в новой версии должно быть не меньше этой доли от старой
FAILED_RETRY_HOURS = 6        # после неудачного запуска следующая попытка раньше обычной
USER_AGENT = "schedule-bot/1.0 (timetable sync, at most once a day)"

XLS_MAGIC = b"\xd0\xcf\x11\xe0"
XLSX_MAGIC = b"PK\x03\x04"


class SyncError(Exception):
    """Ожидаемая ошибка подгрузки: текст безопасен для показа владельцу."""


def credentials():
    return os.environ.get("LMS_LOGIN", "").strip(), os.environ.get("LMS_PASSWORD", "")


def scrub(text):
    """Убирает логин и пароль из любого текста, который уходит в лог, отчёт или сообщение."""
    text = str(text)
    for secret in credentials():
        if secret and len(secret) >= 3:
            text = text.replace(secret, "***")
    return text


def interval_hours():
    try:
        return max(1.0, float(os.environ.get("LMS_SYNC_INTERVAL_HOURS", "24")))
    except ValueError:
        return 24.0


def sync_enabled():
    return os.environ.get("LMS_SYNC_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")


def load_sources(schedule_dir):
    path = os.path.join(schedule_dir, SOURCES_FILE)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def missing_settings(sources):
    """Чего не хватает для работы подгрузки (пустой список: всё есть)."""
    login, password = credentials()
    missing = []
    if not login or not password:
        missing.append("переменные LMS_LOGIN и LMS_PASSWORD на Render")
    if not sources.get("base_url"):
        missing.append(f"base_url в data/{SOURCES_FILE}")
    if not sources.get("crawl") and not sources.get("pages") and not sources.get("resources"):
        missing.append(f"обход категорий (crawl), страницы курса (pages) или ссылки на файлы (resources) в data/{SOURCES_FILE}")
    return missing


# ---------- сайт ----------

LOGINTOKEN_RE = re.compile(r'name=["\']logintoken["\'][^>]*value=["\']([^"\']*)["\']|'
                           r'value=["\']([^"\']*)["\'][^>]*name=["\']logintoken["\']')
CAPTCHA_MARKERS = ("g-recaptcha", "recaptcha/api", "h-captcha", "hcaptcha")
ANCHOR_RE = re.compile(r'<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.DOTALL | re.IGNORECASE)
RESOURCE_HREF_RE = re.compile(r"/mod/resource/view\.php\?id=\d+")
PLUGINFILE_RE = re.compile(r'(?:href|src|data)=["\']([^"\']*pluginfile\.php[^"\']*)["\']', re.IGNORECASE)
ACCESSHIDE_RE = re.compile(r"<span[^>]*accesshide[^>]*>.*?</span>", re.DOTALL | re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")


def _text(fragment):
    fragment = ACCESSHIDE_RE.sub("", fragment)
    return re.sub(r"\s+", " ", html.unescape(TAG_RE.sub("", fragment))).strip()


def is_login_page(text):
    return "logintoken" in text and ('id="login"' in text or "name=\"username\"" in text or "name='username'" in text)


def find_resources(page_html, page_url):
    """Файлы на странице курса: [(название, ссылка)]. Ищем ссылки на ресурсы-файлы (mod/resource) и
    прямые ссылки на pluginfile.php; название берём из текста ссылки, id в ссылке значения не имеет."""
    found, seen = [], set()
    for href, inner in ANCHOR_RE.findall(page_html):
        href = html.unescape(href)
        if RESOURCE_HREF_RE.search(href) or "pluginfile.php" in href:
            url = urljoin(page_url, href)
            name = _text(inner) or unquote(os.path.basename(urlparse(url).path))
            if url not in seen:
                seen.add(url)
                found.append((name, url))
    return found


CATEGORY_HREF_RE = re.compile(r"/course/index\.php\?categoryid=(\d+)")
COURSE_HREF_RE = re.compile(r"/course/view\.php\?id=(\d+)")


def main_region(page_html):
    """Основная часть страницы без шапки, хлебных крошек и боковых меню: оттуда нельзя уйти «вверх» по ссылкам."""
    i = page_html.find('id="region-main"')
    return page_html[i:] if i >= 0 else page_html


def parse_listing(page_html, page_url):
    """Страница категории или курса: (подкатегории, курсы, файлы), каждый список [(название, ссылка)]."""
    region = main_region(page_html)
    categories, courses, seen = [], [], set()
    for href, inner in ANCHOR_RE.findall(region):
        href = html.unescape(href)
        url = urljoin(page_url, href)
        name = _text(inner)
        if not name or url in seen:
            continue
        if CATEGORY_HREF_RE.search(href):
            seen.add(url)
            categories.append((name, url))
        elif COURSE_HREF_RE.search(href):
            seen.add(url)
            courses.append((name, url))
    return categories, courses, find_resources(region, page_url)


def _page_id(url):
    m = CATEGORY_HREF_RE.search(url) or COURSE_HREF_RE.search(url)
    return (("c" if CATEGORY_HREF_RE.search(url) else "k"), m.group(1)) if m else url


def crawl_site(client, cfg, listing, errors):
    """Обход категорий от стартовой страницы вниз по названиям (id не нужны, кроме стартового):
    категории, чьё название подходит под exclude_categories (по умолчанию «заочн»), пропускаются.
    Возвращает найденные файлы [(путь, название, ссылка)]; listing пополняется для /sync list."""
    include = re.compile(cfg["include_categories"], re.IGNORECASE) if cfg.get("include_categories") else None
    exclude = re.compile(cfg.get("exclude_categories", "заочн"), re.IGNORECASE)
    file_include = re.compile(cfg.get("file_include") or ".", re.IGNORECASE)
    file_exclude = re.compile(cfg["file_exclude"], re.IGNORECASE) if cfg.get("file_exclude") else None
    max_depth = int(cfg.get("max_depth", 8))
    max_requests = int(cfg.get("max_requests", 400))
    queue = [(cfg["start"], "", 0)]
    visited, targets, requests = set(), [], 0
    while queue:
        url, path, depth = queue.pop(0)
        page_id = _page_id(url)
        if page_id in visited:
            continue
        visited.add(page_id)
        if requests >= max_requests:
            errors.append(f"обход остановлен: достигнут лимит {max_requests} страниц (max_requests в настройках)")
            break
        requests += 1
        try:
            text, final_url = client.page(url)
        except SyncError as e:
            if "сессия" in str(e):
                raise
            errors.append(f"{path or url}: {e}")
            continue
        categories, courses, resources = parse_listing(text, final_url)
        if resources:
            listing.append((path or url, [name for name, _ in resources]))
        for name, link in resources:
            if file_include.search(name) and not (file_exclude and file_exclude.search(name)):
                targets.append((path, name, link))
        if depth >= max_depth:
            continue
        for name, link in categories:
            if exclude.search(name) or (include and not include.search(name)):
                continue
            queue.append((link, f"{path} / {name}" if path else name, depth + 1))
        for name, link in courses:
            if exclude.search(name) or (include and not include.search(name)):
                continue
            queue.append((link, f"{path} / {name}" if path else name, depth + 1))
    listing.append((f"просмотрено страниц: {requests}", []))
    return targets


def _filename(response):
    disposition = response.headers.get("content-disposition", "")
    m = re.search(r"filename\*\s*=\s*[^']*''([^;]+)", disposition, re.IGNORECASE)
    if m:
        return unquote(m.group(1).strip().strip('"'))
    m = re.search(r'filename\s*=\s*"?([^";]+)"?', disposition, re.IGNORECASE)
    if m:
        name = m.group(1).strip()
        try:
            name = name.encode("latin-1").decode("utf-8")  # заголовки приходят как latin-1
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
        return unquote(name)
    return unquote(os.path.basename(urlparse(str(response.url)).path))


class MoodleClient:
    """Минимальный клиент Moodle: вход по форме и скачивание файлов. Запросы идут по одному с паузой."""

    def __init__(self, base_url, login, password, transport=None, pause=None, sleep=time.sleep):
        self.base_url = base_url.rstrip("/")
        self._login, self._password = login, password
        self._pause = REQUEST_PAUSE if pause is None else pause
        self._sleep = sleep
        self._first = True
        self.http = httpx.Client(follow_redirects=True, timeout=30.0, transport=transport,
                                 headers={"User-Agent": USER_AGENT})

    def close(self):
        self.http.close()

    def _request(self, method, url, **kwargs):
        if not self._first and self._pause:
            self._sleep(self._pause)
        self._first = False
        try:
            return self.http.request(method, urljoin(self.base_url + "/", url), **kwargs)
        except httpx.HTTPError as e:
            raise SyncError(f"сайт недоступен или не ответил ({type(e).__name__})") from None

    def login(self):
        page = self._request("GET", "/login/index.php")
        if page.status_code >= 400:
            raise SyncError(f"страница входа ответила кодом {page.status_code}")
        if any(marker in page.text for marker in CAPTCHA_MARKERS):
            raise SyncError("на странице входа есть капча, автоматический вход по форме невозможен")
        m = LOGINTOKEN_RE.search(page.text)
        token = (m.group(1) or m.group(2)) if m else ""
        data = {"username": self._login, "password": self._password, "anchor": ""}
        if token:
            data["logintoken"] = token
        result = self._request("POST", "/login/index.php", data=data)
        if "login/logout.php" in result.text:
            return
        if any(marker in result.text for marker in CAPTCHA_MARKERS):
            raise SyncError("после ввода пароля сайт требует капчу, автоматический вход невозможен")
        if is_login_page(result.text) or "/login/" in str(result.url):
            raise SyncError("сайт не принял логин и пароль (проверьте LMS_LOGIN и LMS_PASSWORD)")
        raise SyncError("не удалось подтвердить вход: на странице после входа нет ссылки «Выход»")

    def page(self, url):
        response = self._request("GET", url)
        if response.status_code >= 400:
            raise SyncError(f"страница {self._short(url)} ответила кодом {response.status_code}")
        if is_login_page(response.text):
            raise SyncError("сессия не действует: сайт снова показал страницу входа")
        return response.text, str(response.url)

    def download(self, url):
        """(имя файла, содержимое). Ссылка на ресурс может вести на сам файл или на страницу с файлом."""
        response = self._request("GET", url)
        if response.status_code >= 400:
            raise SyncError(f"ссылка {self._short(url)} ответила кодом {response.status_code}")
        if "text/html" in response.headers.get("content-type", "").lower():
            if is_login_page(response.text):
                raise SyncError("сессия не действует: сайт снова показал страницу входа")
            m = PLUGINFILE_RE.search(response.text)
            if not m:
                raise SyncError(f"на странице {self._short(url)} нет файла")
            response = self._request("GET", urljoin(str(response.url), html.unescape(m.group(1))))
            if response.status_code >= 400:
                raise SyncError(f"файл по ссылке {self._short(url)} ответил кодом {response.status_code}")
        content = response.content
        if len(content) > MAX_FILE_BYTES:
            raise SyncError("файл слишком большой")
        return _filename(response), content

    @staticmethod
    def _short(url):
        parsed = urlparse(url)
        return parsed.path + (f"?{parsed.query}" if parsed.query else "")


# ---------- проверка и сохранение ----------

def file_extension(name, content):
    """'.xlsx' или '.xls' по содержимому (имя проверяется, но решает начало файла)."""
    if content.startswith(XLSX_MAGIC):
        return ".xlsx"
    if content.startswith(XLS_MAGIC):
        return ".xls"
    return None


def parse_bytes(content, ext, schedule_dir):
    fd, path = tempfile.mkstemp(suffix=ext)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        return parse_file(path, load_config(schedule_dir))
    finally:
        os.unlink(path)


def group_key(r):
    return f"{r['program']}|{r['course']}|{r['group']}"


def count_groups(records):
    counts = {}
    for r in records:
        counts[group_key(r)] = counts.get(group_key(r), 0) + 1
    return counts


def _group_title(key):
    program, course, name = key.split("|", 2)
    return f"{name} ({program}, {course} курс)"


def check_against_baseline(new_counts, baseline_counts, min_ratio, require_all_groups):
    """Сравнение новой версии с тем, что было. Возвращает текст причины отказа или None."""
    if require_all_groups:
        lost = sorted(set(baseline_counts) - set(new_counts))
        if lost:
            names = ", ".join(_group_title(k) for k in lost[:5]) + (" и др." if len(lost) > 5 else "")
            return f"из файла пропали группы: {names}"
    common = set(new_counts) & set(baseline_counts)
    old_total = sum(baseline_counts[k] for k in common)
    new_total = sum(new_counts[k] for k in common)
    if old_total and new_total < old_total * min_ratio:
        return f"занятий стало {new_total} вместо {old_total}, это слишком резкое падение"
    return None


def disk_records(schedule_dir):
    return parse_all(schedule_dir)


def merged_records(conn, schedule_dir):
    """Расписание для бота: файлы из папки data/ (репозиторий и загрузки владельца), поверх них
    группы из свежей подгрузки. Подгруженный файл, который не читается, пропускается."""
    records = disk_records(schedule_dir)
    override = {}
    for item in list_synced_files(conn, with_content=True):
        try:
            ext = file_extension(item["file_name"], item["content"])
            if not ext:
                raise ValueError("не таблица Excel")
            for r in parse_bytes(item["content"], ext, schedule_dir):
                override.setdefault(group_key(r), []).append(r)
        except Exception:
            logger.exception("подгруженный файл %s не читается, использую запасные источники", item["key"])
    if not override:
        return records
    return [r for r in records if group_key(r) not in override] + [r for rs in override.values() for r in rs]


# ---------- запуск ----------

class Report:
    def __init__(self):
        self.lines = []
        self.problems = []   # то, о чём нужно сообщить владельцу
        self.changed = False

    def ok(self, text):
        self.lines.append(f"✅ {text}")

    def info(self, text):
        self.lines.append(text)

    def problem(self, text):
        self.lines.append(f"⚠️ {text}")
        self.problems.append(text)

    def text(self):
        return scrub("\n".join(self.lines))

    def signature(self):
        return scrub(" | ".join(self.problems))


def _now():
    return datetime.now(timezone.utc)


def is_due(conn, now=None):
    """Пора ли запускать плановую подгрузку: раз в LMS_SYNC_INTERVAL_HOURS часов (по умолчанию 24),
    после неудачного запуска повтор раньше."""
    now = now or _now()
    last = get_sync_state(conn, "last_run")
    if not last:
        return True
    wait = interval_hours() if get_sync_state(conn, "last_ok") == "1" else min(interval_hours(), FAILED_RETRY_HOURS)
    return now - datetime.fromisoformat(last) >= timedelta(hours=wait)


def _fetch_all(sources, client_factory, list_only):
    """Сетевая часть (идёт в отдельном потоке): вход, поиск файлов, скачивание.
    Возвращает (downloads, listing, errors): downloads [(название, имя файла, байты)]."""
    login, password = credentials()
    client = client_factory(sources["base_url"], login, password)
    downloads, listing, errors = [], [], []
    try:
        client.login()
        targets = []
        for page in sources.get("pages", []):
            try:
                text, url = client.page(page["url"])
            except SyncError as e:
                if "сессия" in str(e):
                    raise
                errors.append(str(e))
                continue
            include = re.compile(page.get("include") or ".", re.IGNORECASE)
            exclude = re.compile(page["exclude"], re.IGNORECASE) if page.get("exclude") else None
            names = find_resources(text, url)
            listing.append((page["url"], [name for name, _ in names]))
            for name, link in names:
                if include.search(name) and not (exclude and exclude.search(name)):
                    targets.append((name, link))
        if sources.get("crawl"):
            for path, name, link in crawl_site(client, sources["crawl"], listing, errors):
                targets.append((name, link))
        for resource in sources.get("resources", []):
            targets.append((resource.get("label") or resource["url"], resource["url"]))
        if not list_only:
            seen = set()
            for name, link in targets:
                if link in seen:
                    continue
                seen.add(link)
                try:
                    file_name, content = client.download(link)
                    downloads.append((name, file_name, content))
                except SyncError as e:
                    errors.append(f"«{name}»: {e}")
    finally:
        client.close()
    return downloads, listing, errors


async def run_sync(conn, schedule_dir, list_only=False, client_factory=None):
    """Один запуск подгрузки. Возвращает Report; report.changed: в базе появилась новая версия файлов."""
    report = Report()
    try:
        sources = load_sources(schedule_dir)
    except Exception as e:
        report.problem(f"не читается data/{SOURCES_FILE}: {type(e).__name__}")
        return report
    missing = missing_settings(sources)
    if missing:
        report.info("Подгрузка не настроена. Не хватает: " + "; ".join(missing) + ".")
        return report

    factory = client_factory or (lambda base, login, password: MoodleClient(base, login, password))
    try:
        downloads, listing, errors = await asyncio.to_thread(_fetch_all, sources, factory, list_only)
    except SyncError as e:
        report.problem(f"подгрузка не удалась: {e}. Бот работает на запасных источниках.")
        return report
    except Exception as e:
        logger.error("подгрузка: неожиданная ошибка %s", scrub(f"{type(e).__name__}: {e}"))
        report.problem(f"подгрузка не удалась: непредвиденная ошибка ({type(e).__name__}). Бот работает на запасных источниках.")
        return report

    if list_only:
        for url, names in listing:
            if not names and url.startswith("просмотрено"):
                report.info(url)
                continue
            report.info(f"{url}: файлов {len(names)}")
            report.lines.extend(f"  • {name}" for name in names[:60])
        for error in errors:
            report.problem(error)
        if not listing and not errors:
            report.info("Страниц для просмотра в настройках нет.")
        return report

    for error in errors:
        report.problem(error)
    if not downloads and not errors:
        report.problem("ни одного подходящего файла не нашлось: проверьте include/exclude и ссылки в настройках. Бот работает на запасных источниках.")

    min_ratio = float(sources.get("min_ratio", DEFAULT_MIN_RATIO))
    stored = {item["key"]: item for item in list_synced_files(conn)}
    disk_counts = None
    for name, file_name, content in downloads:
        try:
            disk_counts = await _process(conn, schedule_dir, report, stored, name, file_name, content,
                                         min_ratio, disk_counts)
        except Exception as e:
            logger.error("подгрузка: файл %s: %s", file_name, scrub(f"{type(e).__name__}: {e}"))
            report.problem(f"{file_name}: не удалось обработать ({type(e).__name__}), осталась прошлая версия или запасной источник")
    return report


async def _process(conn, schedule_dir, report, stored, name, file_name, content, min_ratio, disk_counts):
    ext = file_extension(file_name, content)
    if not ext:
        report.problem(f"{file_name}: это не таблица Excel (.xls/.xlsx), пропущен")
        return disk_counts
    key = file_name.lower()
    sha = hashlib.sha256(content).hexdigest()
    previous = stored.get(key)
    if previous and previous["sha256"] == sha:
        report.info(f"{file_name}: без изменений")
        return disk_counts

    try:
        records = await asyncio.to_thread(parse_bytes, content, ext, schedule_dir)
    except Exception as e:
        report.problem(f"{file_name}: не читается ({scrub(str(e))[:150]}), оставлена прошлая версия или запасной источник")
        return disk_counts
    if not records:
        report.problem(f"{file_name}: в файле нет ни одного занятия, оставлена прошлая версия или запасной источник")
        return disk_counts

    new_counts = count_groups(records)
    if previous:
        baseline, require_all = json.loads(previous["group_counts"]), True
    else:
        # первого скачивания этого файла в базе ещё нет: сравниваем с тем, что бот показывает сейчас
        if disk_counts is None:
            disk_counts = count_groups(await asyncio.to_thread(disk_records, schedule_dir))
        baseline, require_all = {k: v for k, v in disk_counts.items() if k in new_counts}, False
    reason = check_against_baseline(new_counts, baseline, min_ratio, require_all) if baseline else None
    if reason:
        report.problem(f"{file_name}: не принят, {reason}. Осталась прошлая версия или запасной источник")
        return disk_counts

    save_synced_file(conn, key, file_name, content, sha, _now().isoformat(timespec="seconds"),
                     json.dumps(new_counts, ensure_ascii=False), len(records))
    superseded = []
    for other_key, item in stored.items():
        if other_key != key and set(json.loads(item["group_counts"])) <= set(new_counts):
            delete_synced_file(conn, other_key)  # старый файл целиком покрыт новым: историю не копим
            superseded.append(item["file_name"])
    stored[key] = {"key": key, "file_name": file_name, "sha256": sha, "group_counts": json.dumps(new_counts)}
    for gone in superseded:
        stored.pop(gone.lower(), None)
    report.changed = True
    report.ok(f"{file_name}: {'обновлён' if previous else 'новый'}, занятий {len(records)}, групп {len(new_counts)}"
              + (f"; заменил {', '.join(superseded)}" if superseded else ""))
    return disk_counts


def source_summary(conn):
    """Что сейчас берётся из подгрузки: по строке на файл."""
    items = list_synced_files(conn)
    if not items:
        return "Из подгрузки пока ничего нет: расписание берётся из загруженных владельцем файлов и из репозитория."
    lines = ["Из подгрузки (имеют приоритет):"]
    for item in items:
        lines.append(f"  • {item['file_name']}: занятий {item['records']}, получен {item['fetched_at']} UTC")
    return "\n".join(lines)


def record_run(conn, report):
    set_sync_state(conn, "last_run", _now().isoformat(timespec="seconds"))
    set_sync_state(conn, "last_ok", "0" if report.problems else "1")
