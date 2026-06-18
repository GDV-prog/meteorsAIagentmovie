"""
Парсер фильмов с film.ru для проекта Find My Movie.

Диапазон: 2019-2026, цель ~1250 фильмов, равномерно по годам (берём с запасом).

Две фазы:
  1) collect_links() — по годам листаем каталог ?year=Y&page=N,
     собираем уникальные ссылки на фильмы -> data/links.csv
  2) parse_details() — заходим на каждую страницу, читаем JSON-LD (@type: Movie)
     -> строка в data/raw.csv

Особенности (требования задания):
  - requests + BeautifulSoup
  - задержки между запросами (time.sleep) — не кладём сайт
  - возобновляемость: links.csv и raw.csv дописываются; уже обработанные URL пропускаются
  - ошибки логируются, на битых страницах не падаем

Запуск:
    uv run python parser.py            # обе фазы (с прогресс-баром)
    uv run python parser.py links      # только сбор ссылок
    uv run python parser.py details    # только сбор карточек
    uv run python parser.py status     # одноразовый снимок прогресса (бар)
    uv run python parser.py watch      # живой бар: следить за фоновым запуском
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import sys
import time

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

# ---------------------------------------------------------------- настройки ---
YEARS = list(range(2019, 2027))          # 2019..2026
LINKS_PER_YEAR = 190                      # с запасом: ~190*8 ~ 1520 -> ~1250 чистых
PER_PAGE = 30                             # film.ru отдаёт 30 фильмов на страницу
MAX_PAGES_PER_YEAR = 15                   # предохранитель от бесконечного листания

DATA_DIR = "data"
LINKS_CSV = os.path.join(DATA_DIR, "links.csv")
RAW_CSV = os.path.join(DATA_DIR, "raw.csv")
LOG_FILE = os.path.join(DATA_DIR, "parser.log")

BASE = "https://www.film.ru"
CATALOG = BASE + "/a-z/movies?year={year}&page={page}"

SLEEP_LIST = 1.5     # пауза между страницами каталога
SLEEP_DETAIL = 1.0   # пауза между страницами фильмов
TIMEOUT = 20
RETRIES = 3

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ru,en;q=0.9",
}

FIELDS = [
    "title", "original_title", "year", "genres", "director", "cast",
    "country", "rating", "num_ratings", "duration_min", "description",
    "poster_url", "url",
]

# ----------------------------------------------------------------- логирование
class TqdmLoggingHandler(logging.Handler):
    """Пишет логи через tqdm.write — чтобы не рвать строку прогресс-бара."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            tqdm.write(self.format(record))
        except Exception:
            self.handleError(record)


os.makedirs(DATA_DIR, exist_ok=True)
_console = TqdmLoggingHandler()
_console.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s"))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), _console],
)
log = logging.getLogger("parser")

session = requests.Session()
session.headers.update(HEADERS)


def fetch(url: str) -> str | None:
    """GET с ретраями. Возвращает текст или None (не падаем)."""
    for attempt in range(1, RETRIES + 1):
        try:
            r = session.get(url, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.text
            log.warning("HTTP %s на %s (попытка %s)", r.status_code, url, attempt)
        except requests.RequestException as e:
            log.warning("Ошибка запроса %s: %s (попытка %s)", url, e, attempt)
        time.sleep(2 * attempt)
    log.error("Не удалось скачать %s после %s попыток", url, RETRIES)
    return None


# ============================================================ ФАЗА 1: ссылки ==
def extract_movie_links(html: str) -> list[str]:
    """Уникальные абсолютные ссылки /movies/<slug> со страницы каталога."""
    soup = BeautifulSoup(html, "lxml")
    slugs: list[str] = []
    seen: set[str] = set()
    for a in soup.select('a[href^="/movies/"]'):
        href = a.get("href", "")
        m = re.fullmatch(r"/movies/([a-z0-9-]+)", href)
        if m and href not in seen:
            seen.add(href)
            slugs.append(BASE + href)
    return slugs


def collect_links() -> None:
    """Собираем ~LINKS_PER_YEAR ссылок на каждый год -> links.csv (возобновляемо)."""
    have: dict[int, set[str]] = {y: set() for y in YEARS}
    if os.path.exists(LINKS_CSV):
        with open(LINKS_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                y = int(row["year"])
                have.setdefault(y, set()).add(row["url"])
        log.info("links.csv уже есть: %s ссылок", sum(len(s) for s in have.values()))

    new_exists = os.path.exists(LINKS_CSV)
    out = open(LINKS_CSV, "a", newline="", encoding="utf-8")
    writer = csv.writer(out)
    if not new_exists:
        writer.writerow(["year", "url"])

    target = LINKS_PER_YEAR * len(YEARS)
    bar = tqdm(
        total=target,
        initial=sum(len(s) for s in have.values()),
        desc="Ссылки",
        unit="url",
        ncols=90,
    )
    for year in YEARS:
        bar.set_postfix(year=year)
        if len(have[year]) >= LINKS_PER_YEAR:
            continue
        for page in range(1, MAX_PAGES_PER_YEAR + 1):
            if len(have[year]) >= LINKS_PER_YEAR:
                break
            html = fetch(CATALOG.format(year=year, page=page))
            if not html:
                break
            links = extract_movie_links(html)
            if not links:
                break
            for url in links:
                if url not in have[year]:
                    have[year].add(url)
                    writer.writerow([year, url])
                    bar.update(1)
            out.flush()
            time.sleep(SLEEP_LIST)
    bar.close()

    out.close()
    total = sum(len(s) for s in have.values())
    log.info("ФАЗА 1 готова: %s ссылок в %s", total, LINKS_CSV)


# =========================================================== ФАЗА 2: карточки ==
def iso_duration_to_min(value: str | None) -> str:
    """'PT1H50M' -> '110'. Пусто если не распарсилось."""
    if not value:
        return ""
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value)
    if not m:
        return ""
    h, mm, _ = (int(x) if x else 0 for x in m.groups())
    total = h * 60 + mm
    return str(total) if total else ""


def find_json_ld_movie(soup: BeautifulSoup) -> dict | None:
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for d in candidates:
            if isinstance(d, dict) and d.get("@type") == "Movie":
                return d
    return None


def names(value) -> str:
    """[{'name': ...}, ...] или ['..'] -> 'a; b; c'."""
    if not value:
        return ""
    if isinstance(value, str):
        return value
    parts = []
    for item in value:
        if isinstance(item, dict):
            parts.append(item.get("name", ""))
        else:
            parts.append(str(item))
    return "; ".join(p for p in parts if p)


def extract_original_title(ld: dict, title: str) -> str:
    """Оригинальное название из JSON-LD (alternateName / alternativeHeadline).
    У российских фильмов этих полей нет -> пусто (это корректно)."""
    for key in ("alternateName", "alternativeHeadline"):
        val = ld.get(key)
        if isinstance(val, str) and val.strip() and val.strip() != title:
            return val.strip()
    return ""


def parse_movie(url: str, html: str) -> dict | None:
    soup = BeautifulSoup(html, "lxml")
    ld = find_json_ld_movie(soup)
    if not ld:
        log.warning("Нет JSON-LD Movie: %s", url)
        return None

    rating = num_ratings = ""
    agg = ld.get("aggregateRating") or {}
    if isinstance(agg, dict):
        rating = str(agg.get("ratingValue", "") or "")
        num_ratings = str(agg.get("ratingCount", "") or "")

    title = ld.get("name", "") or ""
    return {
        "title": title,
        "original_title": extract_original_title(ld, title),
        "year": str(ld.get("dateCreated", "") or ""),
        "genres": names(ld.get("genre")),
        "director": names(ld.get("director")),
        "cast": names(ld.get("actor")),
        "country": names(ld.get("countryOfOrigin")),
        "rating": rating,
        "num_ratings": num_ratings,
        "duration_min": iso_duration_to_min(ld.get("duration")),
        "description": (ld.get("description", "") or "").strip(),
        "poster_url": ld.get("image", "") or "",
        "url": ld.get("url", "") or url,
    }


def parse_details() -> None:
    """Читаем links.csv, парсим карточки -> raw.csv (пропускаем уже готовые)."""
    if not os.path.exists(LINKS_CSV):
        log.error("Нет %s — сначала запусти фазу сбора ссылок", LINKS_CSV)
        return

    with open(LINKS_CSV, newline="", encoding="utf-8") as f:
        urls = [row["url"] for row in csv.DictReader(f)]
    # дедуп ссылок между годами, порядок сохраняем
    urls = list(dict.fromkeys(urls))

    done: set[str] = set()
    if os.path.exists(RAW_CSV):
        with open(RAW_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                done.add(row["url"])
        log.info("raw.csv уже есть: %s фильмов — докачиваем", len(done))

    raw_exists = os.path.exists(RAW_CSV)
    out = open(RAW_CSV, "a", newline="", encoding="utf-8")
    writer = csv.DictWriter(out, fieldnames=FIELDS)
    if not raw_exists:
        writer.writeheader()

    todo = [u for u in urls if u not in done]
    log.info("К обработке: %s фильмов (уже готово %s из %s)", len(todo), len(done), len(urls))

    saved = 0
    bar = tqdm(
        todo,
        desc="Карточки",
        unit="фильм",
        initial=0,
        total=len(todo),
        ncols=90,
    )
    for url in bar:
        html = fetch(url)
        if html:
            row = parse_movie(url, html)
            if row:
                writer.writerow(row)
                out.flush()
                saved += 1
        bar.set_postfix(saved=saved, all=len(done) + saved)
        time.sleep(SLEEP_DETAIL)
    bar.close()

    out.close()
    total = len(done) + saved
    log.info("ФАЗА 2 готова: +%s, всего %s фильмов в %s", saved, total, RAW_CSV)


# ============================================================ СТАТУС / WATCH ==
def _count_rows(path: str) -> int:
    if not os.path.exists(path):
        return 0
    with open(path, newline="", encoding="utf-8") as f:
        return max(0, sum(1 for _ in f) - 1)  # минус заголовок


def show_status() -> None:
    """Одноразовый снимок прогресса баром (можно вызывать при работающем фоне)."""
    links = _count_rows(LINKS_CSV)
    raw = _count_rows(RAW_CSV)
    target = LINKS_PER_YEAR * len(YEARS)

    print(f"Цель: ~{target} ссылок -> {len(YEARS)} лет {YEARS[0]}-{YEARS[-1]}\n")
    print(tqdm.format_meter(links, max(target, links) or 1, 0, ncols=90, prefix="Ссылки  ", unit="url"))
    print(tqdm.format_meter(raw, links or 1, 0, ncols=90, prefix="Карточки", unit="фильм"))
    print(f"\nlinks.csv: {links}   raw.csv: {raw}")


def watch_status() -> None:
    """Живой бар по карточкам — следим за фоновым запуском, пока он не закончит."""
    while _count_rows(LINKS_CSV) == 0:
        print("Жду появления links.csv ...")
        time.sleep(3)
    total = _count_rows(LINKS_CSV)
    bar = tqdm(total=total, initial=_count_rows(RAW_CSV), desc="Карточки",
               unit="фильм", ncols=90)
    try:
        while True:
            raw = _count_rows(RAW_CSV)
            total = _count_rows(LINKS_CSV)  # фаза 1 могла дособрать ссылки
            bar.total = total
            bar.n = raw
            bar.refresh()
            if raw >= total and total > 0:
                break
            time.sleep(3)
    except KeyboardInterrupt:
        pass
    finally:
        bar.close()


# ----------------------------------------------------------------------- main -
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    if mode == "status":
        show_status()
    elif mode == "watch":
        watch_status()
    else:
        if mode in ("links", "all"):
            collect_links()
        if mode in ("details", "all"):
            parse_details()
