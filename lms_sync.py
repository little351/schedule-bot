"""Автоподгрузка файлов расписания с сайта вуза (Moodle).

Схема: войти по логину и паролю (форма входа Moodle с logintoken), найти файлы по названию
ресурса на страницах курса из data/lms_sources.json (id ссылок на файлы могут меняться, поэтому
жёсткие id не нужны), скачать, проверить парсером и сохранить в базу как последнюю хорошую версию.

Логин и пароль берутся только из переменных окружения LMS_LOGIN и LMS_PASSWORD. Они не пишутся
ни в лог, ни в отчёты: любой текст перед выводом проходит через scrub().

Порядок источников расписания: свежая подгрузка (эта база) > файлы, загруженные владельцем > файлы из репозитория.
"""
import asyncio
import gc
import gzip
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
    LOCAL_TZ, delete_synced_file, get_sync_state, get_synced_content, list_synced_files, list_synced_parsed,
    save_synced_file, set_sync_state, set_synced_parsed,
)
import schedule_parsers
from schedule_parsers import list_schedule_files, parse_all, parse_file
from schedule_parsers.common import load_config

logger = logging.getLogger(__name__)

SOURCES_FILE = "lms_sources.json"
REQUEST_PAUSE = 1.0           # секунд между запросами к сайту: ходим вежливо, по одному
MAX_FILE_BYTES = 20 * 1024 * 1024
DEFAULT_MIN_RATIO = 0.6       # занятий в новой версии должно быть не меньше этой доли от старой
FAILED_RETRY_HOURS = 6        # после неудачного запуска следующая попытка раньше обычной
USER_AGENT = "schedule-bot/1.0 (timetable sync, about once a week)"

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
        return max(1.0, float(os.environ.get("LMS_SYNC_INTERVAL_HOURS", "20")))
    except ValueError:
        return 20.0


def request_timeout():
    try:
        return max(5.0, float(os.environ.get("LMS_TIMEOUT_SECONDS", "30")))
    except ValueError:
        return 30.0


def max_minutes():
    """Общий срок одного запуска: зависшие страницы не должны тянуть подгрузку бесконечно."""
    try:
        return max(1.0, float(os.environ.get("LMS_MAX_MINUTES", "10")))
    except ValueError:
        return 10.0


def memory_mb():
    """Память процесса (МБ) из /proc: видно в отчёте, чтобы следить за расходом на Render."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return None


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
    if not sources.get("pages") and not sources.get("resources"):
        missing.append(f"страницы курсов (pages) или ссылки на файлы (resources) в data/{SOURCES_FILE}")
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
        self.http = httpx.Client(follow_redirects=True, timeout=request_timeout(), transport=transport,
                                 headers={"User-Agent": USER_AGENT})

    def close(self):
        self.http.close()

    def _request(self, method, url, **kwargs):
        if not self._first and self._pause:
            self._sleep(self._pause)
        self._first = False
        target = urljoin(self.base_url + "/", url)
        where = urlparse(target).path + (f"?{urlparse(target).query}" if urlparse(target).query else "")
        for attempt in (1, 2):
            try:
                return self.http.request(method, target, **kwargs)
            except httpx.TimeoutException as e:
                # сайт принял соединение, но молчит: бывает при перегрузке или когда он закрыт для зарубежных адресов
                if attempt == 1 and method == "GET":
                    continue  # один повтор безопасного запроса, дальше сдаёмся
                raise SyncError(f"сайт не ответил на запрос {where} ({type(e).__name__}, дважды по {request_timeout():.0f} с)"
                                if method == "GET" else
                                f"сайт не ответил на запрос {where} ({type(e).__name__})") from None
            except httpx.HTTPError as e:
                raise SyncError(f"не удалось связаться с сайтом на запросе {where} ({type(e).__name__})") from None

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


# Готовые записи файлов из data/, собранные заранее (python parser.py --build-cache): старт не разбирает
# 13 МБ таблиц заново. Файл скрытый, поэтому как расписание не читается. Ключ - sha256 содержимого файла:
# новый или изменённый файл (в том числе загруженный владельцем) просто разбирается как раньше.
PARSED_CACHE_FILE = ".parsed-cache.json.gz"


def _file_sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def build_parsed_cache(schedule_dir):
    """Собирает кеш разобранных файлов data/; возвращает число файлов в нём."""
    config = load_config(schedule_dir)
    files = {}
    for path in list_schedule_files(schedule_dir):
        files[_file_sha(path)] = parse_file(path, config)
    payload = {"version": parsed_version(schedule_dir), "files": files}
    data = json.dumps(payload, default=str, ensure_ascii=False, sort_keys=True).encode()
    with open(os.path.join(schedule_dir, PARSED_CACHE_FILE), "wb") as f:
        f.write(gzip.compress(data, mtime=0))  # mtime=0: одинаковые данные дают одинаковый файл в git
    return len(files)


def load_parsed_cache(schedule_dir):
    """{sha256 файла: записи} из кеша; пусто, если кеша нет, он испорчен или парсеры с тех пор поменялись."""
    try:
        with open(os.path.join(schedule_dir, PARSED_CACHE_FILE), "rb") as f:
            payload = json.loads(gzip.decompress(f.read()))
    except (OSError, ValueError, EOFError):
        return {}
    if not isinstance(payload, dict) or payload.get("version") != parsed_version(schedule_dir):
        return {}
    return payload.get("files") or {}


def disk_records(schedule_dir):
    """Как parse_all(schedule_dir), но файлы, которые есть в кеше, не разбираются заново.
    Парсеры (schedule_parsers) нарочно не трогаем: их отпечаток хранится вместе с готовым расписанием
    подгруженных файлов в базе, и любая правка в них заставила бы бота перекачать эти файлы из Neon."""
    cache = load_parsed_cache(schedule_dir)
    paths = list_schedule_files(schedule_dir)
    shas = [_file_sha(path) for path in paths] if cache else []
    if not cache or any(sha not in cache for sha in shas):
        return parse_all(schedule_dir)  # новый или изменённый файл: разбираем всё, как раньше
    # дальше то же, что в parse_all: группа из нескольких файлов берётся из последнего по пути
    by_file = [(path, cache[sha]) for path, sha in zip(paths, shas)]
    source = {}
    for path, records in by_file:
        for r in records:
            source[(r["program"], r["course"], r["group"])] = path
    return [r for path, records in by_file for r in records if source[(r["program"], r["course"], r["group"])] == path]


def active_keys(conn):
    """Файлы, которые были на страницах курсов при последней подгрузке; None, если подгрузка так не запускалась."""
    raw = get_sync_state(conn, "active_keys")
    if not raw:
        return None
    try:
        return set(json.loads(raw))
    except ValueError:
        return None


def parsed_version(schedule_dir):
    """Отпечаток парсеров и data/programs.json: если они поменялись, готовое расписание из базы
    устарело и файл нужно разобрать заново."""
    h = hashlib.sha256()
    parsers_dir = os.path.dirname(os.path.abspath(schedule_parsers.__file__))
    for name in sorted(os.listdir(parsers_dir)):
        if name.endswith(".py"):
            with open(os.path.join(parsers_dir, name), "rb") as f:
                h.update(name.encode() + b"\0" + f.read())
    h.update(json.dumps(load_config(schedule_dir), sort_keys=True, ensure_ascii=False).encode())
    return h.hexdigest()


def encode_records(records):
    # дата дальше нужна только строкой (str(r["date"]) в save_schedule), поэтому и хранится строкой
    return gzip.compress(json.dumps(records, default=str, ensure_ascii=False).encode())


def decode_records(blob):
    return json.loads(gzip.decompress(blob))


def synced_file_records(conn, item, schedule_dir, version):
    """Занятия подгруженного файла: из готового расписания в базе, а если его нет или парсеры поменялись,
    файл скачивается из базы один раз, разбирается и готовое расписание сохраняется обратно."""
    if item["parsed"] is not None and item["parsed_version"] == version:
        return decode_records(item["parsed"])
    content = get_synced_content(conn, item["key"])
    ext = file_extension(item["file_name"], content or b"")
    if not ext:
        raise ValueError("не таблица Excel")
    records = parse_bytes(content, ext, schedule_dir)
    set_synced_parsed(conn, item["key"], encode_records(records), version)
    return records


def merged_records(conn, schedule_dir):
    """Расписание для бота: файлы из папки data/ (репозиторий и загрузки владельца), поверх них
    группы из свежей подгрузки. Подгруженный файл, который не читается, пропускается.
    Сами файлы из базы не качаются (это десятки МБ трафика на каждый старт), берётся готовое расписание."""
    records = disk_records(schedule_dir)
    override = {}
    active = active_keys(conn)
    version = parsed_version(schedule_dir)
    for item in list_synced_parsed(conn):
        if active is None or item["key"] not in active:
            continue  # файл остался от прошлых запусков и сейчас на страницах курсов не лежит
        try:
            for r in synced_file_records(conn, item, schedule_dir, version):
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
        self.downloaded = 0  # сколько файлов скачано с сайта: если хоть что-то, расписание проверено

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
    """Пора ли запускать плановую подгрузку: раз в LMS_SYNC_INTERVAL_HOURS часов (по умолчанию 20, то есть раз в день:
    запас в 4 часа, чтобы запуск не уезжал на час позже каждый день и не пропадал, пока сервис спит),
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
    deadline = time.monotonic() + max_minutes() * 60
    try:
        client.login()
        targets = []
        for page in sources.get("pages", []):
            if time.monotonic() > deadline:
                errors.append(f"страницы после «{page.get('label') or page['url']}» не просмотрены: вышел срок {max_minutes():.0f} мин")
                break
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
            listing.append((page.get("label") or page["url"], [name for name, _ in names]))
            for name, link in names:
                if include.search(name) and not (exclude and exclude.search(name)):
                    targets.append((name, link))
        for resource in sources.get("resources", []):
            targets.append((resource.get("label") or resource["url"], resource["url"]))
        if not list_only:
            seen = set()
            for name, link in targets:
                if link in seen:
                    continue
                seen.add(link)
                if time.monotonic() > deadline:
                    errors.append(f"файлы после «{name}» не скачаны: вышел срок {max_minutes():.0f} мин")
                    break
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
            report.info(f"{url}: файлов {len(names)}")
            report.lines.extend(f"  • {name}" for name in names[:60])
        for error in errors:
            report.problem(error)
        if not listing and not errors:
            report.info("Страниц для просмотра в настройках нет.")
        return report

    for error in errors:
        report.problem(error)
    report.downloaded = len(downloads)
    report.info(f"Скачано файлов: {len(downloads)}, память процесса: {memory_mb() or '?'} МБ")
    if not downloads and not errors:
        report.problem("ни одного подходящего файла не нашлось: проверьте include/exclude и ссылки в настройках. Бот работает на запасных источниках.")

    min_ratio = float(sources.get("min_ratio", DEFAULT_MIN_RATIO))
    stored = {item["key"]: item for item in list_synced_files(conn)}
    on_pages = {file_name.lower() for _, file_name, _ in downloads}
    disk_counts = None
    for name, file_name, content in downloads:
        try:
            disk_counts = await _process(conn, schedule_dir, report, stored, name, file_name, content,
                                         min_ratio, disk_counts, on_pages)
        except Exception as e:
            logger.error("подгрузка: файл %s: %s", file_name, scrub(f"{type(e).__name__}: {e}"))
            report.problem(f"{file_name}: не удалось обработать ({type(e).__name__}), осталась прошлая версия или запасной источник")
    stored_now = {item["key"] for item in list_synced_files(conn)}
    seen = {file_name.lower() for _, file_name, content in downloads} & stored_now
    if seen:
        previous = active_keys(conn)
        if errors and previous:
            seen |= previous  # часть страниц не открылась: прежние файлы не выбрасываем
        if previous != seen:
            set_sync_state(conn, "active_keys", json.dumps(sorted(seen), ensure_ascii=False))
            report.changed = True
    gc.collect()
    report.info(f"Память после обработки: {memory_mb() or '?'} МБ")
    return report


async def _process(conn, schedule_dir, report, stored, name, file_name, content, min_ratio, disk_counts, on_pages=()):
    ext = file_extension(file_name, content)
    if not ext:
        report.info(f"{file_name}: не таблица Excel (.xls/.xlsx), пропущен")
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
    set_synced_parsed(conn, key, encode_records(records), parsed_version(schedule_dir))
    superseded = []
    for other_key, item in stored.items():
        # старый файл целиком покрыт новым: историю не копим. Файл, который сейчас тоже лежит на страницах
        # (например, отдельное расписание ФОД для тех же групп), не трогаем: это не старая версия, а второй файл
        if other_key != key and other_key not in on_pages and set(json.loads(item["group_counts"])) <= set(new_counts):
            delete_synced_file(conn, other_key)
            superseded.append(item["file_name"])
    stored[key] = {"key": key, "file_name": file_name, "sha256": sha, "group_counts": json.dumps(new_counts)}
    for gone in superseded:
        stored.pop(gone.lower(), None)
    report.changed = True
    report.ok(f"{file_name}: {'обновлён' if previous else 'новый'}, занятий {len(records)}, групп {len(new_counts)}"
              + (f"; заменил {', '.join(superseded)}" if superseded else ""))
    return disk_counts


def _moscow_time(iso):
    """'2026-10-20T20:15:00+00:00' -> '20.10.2026 23:15 МСК' (как есть, если не разбирается)."""
    try:
        moment = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return f"{iso} UTC"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(LOCAL_TZ).strftime("%d.%m.%Y %H:%M МСК")


def source_summary(conn):
    """Что сейчас берётся из подгрузки: по строке на файл (только файлы, которые бот использует)."""
    active = active_keys(conn)
    items = [item for item in list_synced_files(conn) if active is None or item["key"] in active]
    if not items:
        return "Из подгрузки пока ничего нет: расписание берётся из загруженных владельцем файлов и из репозитория."
    lines = ["Из подгрузки (имеют приоритет):"]
    for item in items:
        lines.append(f"  • {item['file_name']}: занятий {item['records']}, получен {_moscow_time(item['fetched_at'])}")
    return "\n".join(lines)


def record_run(conn, report):
    set_sync_state(conn, "last_run", _now().isoformat(timespec="seconds"))
    set_sync_state(conn, "last_ok", "0" if report.problems else "1")
    if report.downloaded:
        status["checked_at"] = _now().isoformat(timespec="seconds")
        save_status(conn)


# ---------- что изменилось (для мини-аппа) ----------
# Хранится одной короткой строкой в sync_state и держится в памяти: мини-апп спрашивает расписание часто,
# а база (Neon) должна читаться как можно реже.
STATUS_KEY = "schedule_status"
CHANGES_KEEP_DAYS = 14
CHANGED_DATES_MAX = 5
status = {"checked_at": None, "changed": {}}  # changed: ключ группы -> {"at": время, "dates": ["2026-10-28", ...]}


def load_status(conn):
    try:
        raw = get_sync_state(conn, STATUS_KEY)
        data = json.loads(raw) if raw else {}
    except Exception:
        logger.exception("не удалось прочитать отметку об обновлении расписания")
        return
    status["checked_at"] = data.get("checked_at")
    status["changed"] = data.get("changed") or {}


def save_status(conn):
    try:
        set_sync_state(conn, STATUS_KEY, json.dumps(status, ensure_ascii=False, sort_keys=True))
    except Exception:
        logger.exception("не удалось сохранить отметку об обновлении расписания")


def day_fingerprints(records, today):
    """{ключ группы: {дата: отпечаток пар этого дня}} начиная с today (прошедшие дни не интересны)."""
    days = {}
    for r in records:
        date = str(r["date"])[:10]
        if date >= today:
            days.setdefault(group_key(r), {}).setdefault(date, []).append((str(r["time"]), str(r["subject"])))
    return {key: {date: hashlib.sha1(repr(sorted(lessons)).encode()).hexdigest()[:12]
                  for date, lessons in by_date.items()}
            for key, by_date in days.items()}


def changed_days(before, after, today):
    """Группа -> дни, которые уже были в расписании и поменялись (пары переставили, добавили или убрали).
    Новые недели изменением не считаются, как и пропавшие целиком группы."""
    result = {}
    for key, old_days in before.items():
        new_days = after.get(key)
        if new_days is None:
            continue
        dates = sorted(date for date, mark in old_days.items() if date >= today and new_days.get(date) != mark)
        if dates:
            result[key] = dates
    return result


def note_changes(conn, changes, now=None):
    """Запоминает группы с изменениями (старше CHANGES_KEEP_DAYS дней забываются)."""
    now = now or _now()
    at = now.isoformat(timespec="seconds")
    edge = (now - timedelta(days=CHANGES_KEEP_DAYS)).isoformat(timespec="seconds")
    kept = {key: item for key, item in status["changed"].items() if item.get("at", "") >= edge}
    for key, dates in changes.items():
        kept[key] = {"at": at, "dates": dates[:CHANGED_DATES_MAX]}
    if changes or kept != status["changed"]:
        status["changed"] = kept
        save_status(conn)
