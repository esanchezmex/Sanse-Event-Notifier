#!/usr/bin/env python3
"""Stage 1 ingest scraper for diariodesanse.com.

This script implements:
- source adapter for homepage and category pages
- polite HTTP fetch with retries/backoff and conditional headers
- raw HTML landing under data/raw/{run_id}/
- crawl scope limits (robots + max pages + date window)
- checkpoint + overlap and URL/hash dedup

Usage:
    python scraper/diariodesanse_scraper.py
    python scraper/diariodesanse_scraper.py --max-pages-per-source 3 --overlap-days 2
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://www.diariodesanse.com/"
SOURCE_NAME = "diariodesanse"
DEFAULT_SOURCES = [
    BASE_URL,
    "https://www.diariodesanse.com/fiestas/",
    "https://www.diariodesanse.com/noticias-culturales-de-san-sebastina-de-los-reyes/",
    "https://www.diariodesanse.com/sociedad/",
]

DATE_PATTERNS = (
    re.compile(r"(\d{1,2})\s+([A-Za-záéíóúñ]+),\s+(\d{4})", re.IGNORECASE),
    re.compile(r"(\d{1,2})\s+de\s+([A-Za-záéíóúñ]+)\s+de\s+(\d{4})", re.IGNORECASE),
)

MONTHS_ES = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}

KEYWORD_GROUPS = {
    "event_intent": [
        "festival",
        "feria",
        "encuentro",
        "jornadas",
        "evento gratuito",
        "entrada libre"
    ],
    "activity_atmosphere": [
        "musica en directo",
        "música en directo",
        "djs",
        "conciertos",
        "food trucks",
        "degustacion",
        "degustación",
        "brasa",
        "gastronomía",
        "gastronomia",
        "coctelería"
        "cocteleria"
    ],
    "context_location": [
        "cultura latina",
        "multicultural",
        "tradicion",
        "tradición",
        "familiar",
        "al aire libre",
        "plaza de toros",
    ],
}


@dataclass
class ArticleCandidate:
    url: str
    category_hint: str | None
    list_published_at: datetime | None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc.lower()
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    cleaned = parsed._replace(scheme=scheme, netloc=netloc, path=path, params="", query="", fragment="")
    return urlunparse(cleaned)


def url_sha(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def parse_http_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def parse_spanish_date(text: str) -> datetime | None:
    text_lc = text.lower()
    for pattern in DATE_PATTERNS:
        match = pattern.search(text_lc)
        if not match:
            continue
        day, month_name, year = match.groups()
        month = MONTHS_ES.get(month_name.strip())
        if not month:
            continue
        try:
            return datetime(int(year), int(month), int(day), tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def strip_table_and_non_content(soup: BeautifulSoup) -> None:
    for tag_name in ("script", "style", "noscript", "table", "figure", "img", "iframe"):
        for tag in soup.find_all(tag_name):
            tag.decompose()


def extract_article_body_text(soup: BeautifulSoup) -> str:
    selectors = [
        "article .td-post-content",
        "article .entry-content",
        ".td-post-content",
        ".entry-content",
        "article",
    ]
    for selector in selectors:
        node = soup.select_one(selector)
        if not node:
            continue
        node_copy = BeautifulSoup(str(node), "html.parser")
        strip_table_and_non_content(node_copy)
        text = node_copy.get_text(separator="\n", strip=True)
        if text:
            return text
    logging.info("Selector fallback exhausted for article body extraction")
    body = soup.select_one("body")
    if body:
        body_copy = BeautifulSoup(str(body), "html.parser")
        strip_table_and_non_content(body_copy)
        return body_copy.get_text(separator="\n", strip=True)
    return ""


def extract_article_title(soup: BeautifulSoup) -> str:
    selectors = [
        "article h1.entry-title",
        "h1.entry-title",
        "article h1",
        "h1",
        "meta[property='og:title']",
        "title",
    ]
    for selector in selectors:
        node = soup.select_one(selector)
        if not node:
            continue
        if node.name == "meta":
            content = node.get("content", "").strip()
            if content:
                return content
            continue
        text = node.get_text(strip=True)
        if text:
            return text
    logging.info("Selector fallback exhausted for article title extraction")
    return ""


def extract_article_published_at(soup: BeautifulSoup, fallback: datetime | None = None) -> datetime | None:
    selectors = [
        "time.entry-date",
        "time.td-module-date",
        "meta[property='article:published_time']",
        "meta[name='date']",
    ]
    for selector in selectors:
        node = soup.select_one(selector)
        if not node:
            continue
        if node.name == "time":
            text = (node.get("datetime") or node.get_text(" ", strip=True)).strip()
        else:
            text = node.get("content", "").strip()
        if not text:
            continue
        dt = parse_http_datetime(text)
        if dt:
            return dt
        dt = parse_spanish_date(text)
        if dt:
            return dt

    page_text = soup.get_text(" ", strip=True)
    from_text = parse_spanish_date(page_text[:5000])
    if not from_text and not fallback:
        logging.info("Selector fallback exhausted for article published_at extraction")
    return from_text or fallback


def keyword_signals(title: str, body: str) -> dict[str, object]:
    text = f"{title}\n{body}".lower()
    # Minimal text normalization; intentionally simple for stage 1.
    text = (
        text.replace("á", "a")
        .replace("é", "e")
        .replace("í", "i")
        .replace("ó", "o")
        .replace("ú", "u")
    )
    hits: dict[str, list[str]] = {}
    for group_name, terms in KEYWORD_GROUPS.items():
        found = [term for term in terms if term in text]
        if found:
            hits[group_name] = sorted(set(found))
    flat_hits = sorted({term for values in hits.values() for term in values})
    return {
        "has_plaza_de_toros": "plaza de toros" in text,
        "keyword_hits": flat_hits,
        "keyword_groups_hit": sorted(hits.keys()),
    }


class CheckpointStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self._bootstrap()

    def _bootstrap(self) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS ingest_checkpoint (
                source TEXT PRIMARY KEY,
                last_seen_published_at TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS article_registry (
                url_hash TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                published_at TEXT,
                first_seen_run_id TEXT NOT NULL,
                raw_path TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS http_cache (
                url TEXT PRIMARY KEY,
                etag TEXT,
                last_modified TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.conn.commit()

    def get_checkpoint(self, source: str) -> datetime | None:
        row = self.conn.execute(
            "SELECT last_seen_published_at FROM ingest_checkpoint WHERE source = ?",
            (source,),
        ).fetchone()
        if not row or not row["last_seen_published_at"]:
            return None
        return datetime.fromisoformat(row["last_seen_published_at"])

    def update_checkpoint(self, source: str, last_seen_published_at: datetime | None) -> None:
        now_iso = utc_now().isoformat()
        checkpoint_iso = last_seen_published_at.isoformat() if last_seen_published_at else None
        self.conn.execute(
            """
            INSERT INTO ingest_checkpoint (source, last_seen_published_at, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(source) DO UPDATE SET
                last_seen_published_at = excluded.last_seen_published_at,
                updated_at = excluded.updated_at
            """,
            (source, checkpoint_iso, now_iso),
        )
        self.conn.commit()

    def seen_url_hash(self, hashed_url: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM article_registry WHERE url_hash = ? LIMIT 1",
            (hashed_url,),
        ).fetchone()
        return row is not None

    def register_article(
        self,
        hashed_url: str,
        url: str,
        published_at: datetime | None,
        run_id: str,
        raw_path: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO article_registry
                (url_hash, url, published_at, first_seen_run_id, raw_path, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                hashed_url,
                url,
                published_at.isoformat() if published_at else None,
                run_id,
                raw_path,
                utc_now().isoformat(),
            ),
        )
        self.conn.commit()

    def get_conditional_headers(self, url: str) -> dict[str, str]:
        row = self.conn.execute(
            "SELECT etag, last_modified FROM http_cache WHERE url = ?",
            (url,),
        ).fetchone()
        headers: dict[str, str] = {}
        if not row:
            return headers
        if row["etag"]:
            headers["If-None-Match"] = row["etag"]
        if row["last_modified"]:
            headers["If-Modified-Since"] = row["last_modified"]
        return headers

    def upsert_http_cache(self, url: str, etag: str | None, last_modified: str | None) -> None:
        self.conn.execute(
            """
            INSERT INTO http_cache (url, etag, last_modified, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                etag = excluded.etag,
                last_modified = excluded.last_modified,
                updated_at = excluded.updated_at
            """,
            (url, etag, last_modified, utc_now().isoformat()),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


class DiarioDeSanseScraper:
    def __init__(
        self,
        sources: Iterable[str],
        raw_root: Path,
        db_path: Path,
        overlap_days: int,
        max_pages_per_source: int,
        max_articles_per_run: int,
        min_sleep_seconds: float,
        max_sleep_seconds: float,
        user_agent: str,
        timeout_seconds: int,
    ) -> None:
        self.sources = [normalize_url(s) for s in sources]
        self.raw_root = raw_root
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.run_dir = self.raw_root / self.run_id
        self.overlap_days = overlap_days
        self.max_pages_per_source = max_pages_per_source
        self.max_articles_per_run = max_articles_per_run
        self.min_sleep_seconds = min_sleep_seconds
        self.max_sleep_seconds = max_sleep_seconds
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})
        self.state = CheckpointStore(db_path)
        self.robots = RobotFileParser()
        self.robots_available = False
        self.robots.set_url(urljoin(BASE_URL, "/robots.txt"))
        try:
            self.robots.read()
            self.robots_available = True
        except Exception as exc:
            logging.warning("Could not read robots.txt: %s", exc)

    def _sleep_politely(self) -> None:
        delay = random.uniform(self.min_sleep_seconds, self.max_sleep_seconds)
        time.sleep(delay)

    def _can_fetch(self, url: str) -> bool:
        if not self.robots_available:
            # Fail-open: if robots cannot be fetched (e.g., local SSL chain issue),
            # continue crawling with conservative request limits.
            return True
        return self.robots.can_fetch(self.session.headers["User-Agent"], url)

    def _request_with_retry(self, url: str, conditional_headers: dict[str, str] | None = None) -> requests.Response | None:
        if not self._can_fetch(url):
            logging.info("robots.txt disallows fetch: %s", url)
            return None

        headers = dict(conditional_headers or {})
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                response = self.session.get(url, timeout=self.timeout_seconds, headers=headers)
                if response.status_code in (429, 500, 502, 503, 504):
                    raise requests.HTTPError(f"Transient HTTP {response.status_code}", response=response)
                return response
            except requests.RequestException as exc:
                if attempt == max_attempts:
                    logging.warning("HTTP request failed after %s attempts for %s: %s", attempt, url, exc)
                    return None
                wait_seconds = 2 ** (attempt - 1)
                logging.info("Retrying %s in %ss after error: %s", url, wait_seconds, exc)
                time.sleep(wait_seconds)
        return None

    def _list_page_url(self, source_url: str, page_number: int) -> str:
        if page_number == 1:
            return source_url
        parsed = urlparse(source_url)
        path = parsed.path.rstrip("/")
        paginated_path = f"{path}/page/{page_number}/"
        return urlunparse(parsed._replace(path=paginated_path, query="", fragment=""))

    def _extract_article_candidates(self, html: str) -> list[ArticleCandidate]:
        soup = BeautifulSoup(html, "html.parser")
        candidates: list[ArticleCandidate] = []
        seen_urls: set[str] = set()
        containers = soup.select("article, .td_module_wrap, .td-block-span12, .td-post-template")
        for container in containers:
            anchor = container.select_one("h3 a, h2 a, h1 a, .td-module-title a, a[rel='bookmark'], a.td-image-wrap")
            if not anchor or not anchor.get("href"):
                continue
            url = normalize_url(urljoin(BASE_URL, anchor["href"]))
            if url in seen_urls:
                continue
            seen_urls.add(url)
            category_node = container.select_one(".td-post-category, .entry-category, .meta-category, .td-post-source-tags")
            category_hint = category_node.get_text(" ", strip=True) if category_node else None
            date_node = container.select_one("time, .td-post-date, .entry-date")
            list_dt = parse_spanish_date(date_node.get_text(" ", strip=True)) if date_node else None
            candidates.append(ArticleCandidate(url=url, category_hint=category_hint, list_published_at=list_dt))

        # Fallback: parse headline/bookmark links directly when card containers are absent.
        if not candidates:
            for anchor in soup.select("h3.entry-title a, .td-module-title a, a[rel='bookmark']"):
                href = anchor.get("href")
                if not href:
                    continue
                url = normalize_url(urljoin(BASE_URL, href))
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                container = anchor.find_parent(class_=re.compile(r"(td_module_wrap|td-block-span12|module|post)", re.IGNORECASE))
                category_hint = None
                list_dt = None
                if container:
                    category_node = container.select_one(".td-post-category, .entry-category, .meta-category, .td-post-source-tags")
                    category_hint = category_node.get_text(" ", strip=True) if category_node else None
                    date_node = container.select_one("time, .td-post-date, .entry-date")
                    list_dt = parse_spanish_date(date_node.get_text(" ", strip=True)) if date_node else None
                candidates.append(ArticleCandidate(url=url, category_hint=category_hint, list_published_at=list_dt))
        return candidates

    def _save_raw_html(self, source_tag: str, normalized_url: str, html: str) -> Path:
        url_hash = url_sha(normalized_url)
        date_segment = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        out_dir = self.run_dir / f"source={source_tag}" / f"date={date_segment}"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{url_hash}.html"
        out_path.write_text(html, encoding="utf-8")
        return out_path

    def _should_stop_paging(self, oldest_seen: datetime | None, scan_from: datetime | None) -> bool:
        if not oldest_seen or not scan_from:
            return False
        return oldest_seen < scan_from

    def run(self) -> dict[str, int]:
        checkpoint = self.state.get_checkpoint(SOURCE_NAME)
        scan_from = None
        if checkpoint:
            scan_from = checkpoint - timedelta(days=self.overlap_days)
        logging.info("run_id=%s checkpoint=%s scan_from=%s", self.run_id, checkpoint, scan_from)

        counts = {
            "list_pages_fetched": 0,
            "candidate_urls": 0,
            "article_urls_new": 0,
            "article_urls_skipped_dedup": 0,
            "article_urls_skipped_old": 0,
            "article_pages_fetched": 0,
        }

        max_seen_published_at = checkpoint
        processed_this_run: set[str] = set()

        for source_url in self.sources:
            logging.info("Crawling source: %s", source_url)
            oldest_seen_on_source: datetime | None = None
            for page_num in range(1, self.max_pages_per_source + 1):
                if counts["article_urls_new"] >= self.max_articles_per_run:
                    logging.info("Hit max articles per run (%s), stopping crawl", self.max_articles_per_run)
                    break
                page_url = self._list_page_url(source_url, page_num)
                cond_headers = self.state.get_conditional_headers(page_url)
                list_response = self._request_with_retry(page_url, conditional_headers=cond_headers)
                self._sleep_politely()
                if list_response is None:
                    continue
                if list_response.status_code == 304:
                    logging.info("List page not modified (304): %s", page_url)
                    continue
                if list_response.status_code >= 400:
                    logging.info("Skipping failed list page %s with status=%s", page_url, list_response.status_code)
                    continue
                counts["list_pages_fetched"] += 1
                self.state.upsert_http_cache(
                    page_url,
                    list_response.headers.get("ETag"),
                    list_response.headers.get("Last-Modified"),
                )
                candidates = self._extract_article_candidates(list_response.text)
                counts["candidate_urls"] += len(candidates)
                if not candidates:
                    break

                for candidate in candidates:
                    if counts["article_urls_new"] >= self.max_articles_per_run:
                        logging.info("Hit max articles per run (%s), stopping candidate processing", self.max_articles_per_run)
                        break

                    if candidate.list_published_at:
                        oldest_seen_on_source = (
                            candidate.list_published_at
                            if not oldest_seen_on_source
                            else min(oldest_seen_on_source, candidate.list_published_at)
                        )

                    hashed_url = url_sha(candidate.url)
                    if hashed_url in processed_this_run or self.state.seen_url_hash(hashed_url):
                        counts["article_urls_skipped_dedup"] += 1
                        continue

                    article_cond_headers = self.state.get_conditional_headers(candidate.url)
                    article_response = self._request_with_retry(candidate.url, conditional_headers=article_cond_headers)
                    self._sleep_politely()
                    if article_response is None:
                        continue
                    if article_response.status_code == 304:
                        counts["article_urls_skipped_dedup"] += 1
                        continue
                    if article_response.status_code >= 400:
                        continue
                    counts["article_pages_fetched"] += 1
                    self.state.upsert_http_cache(
                        candidate.url,
                        article_response.headers.get("ETag"),
                        article_response.headers.get("Last-Modified"),
                    )

                    soup = BeautifulSoup(article_response.text, "html.parser")
                    title = extract_article_title(soup)
                    body_text = extract_article_body_text(soup)
                    published_at = extract_article_published_at(soup, fallback=candidate.list_published_at)

                    if scan_from and published_at and published_at < scan_from:
                        counts["article_urls_skipped_old"] += 1
                        continue

                    raw_path = self._save_raw_html("diariodesanse", candidate.url, article_response.text)
                    payload = {
                        "run_id": self.run_id,
                        "source": SOURCE_NAME,
                        "url": candidate.url,
                        "url_hash": hashed_url,
                        "title": title,
                        "body_text": body_text,
                        "published_at": published_at.isoformat() if published_at else None,
                        "fetched_at": utc_now().isoformat(),
                        "category_hint": candidate.category_hint,
                        "raw_html_path": str(raw_path),
                        "keyword_signals": keyword_signals(title, body_text),
                    }
                    json_path = raw_path.with_suffix(".json")
                    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

                    self.state.register_article(
                        hashed_url=hashed_url,
                        url=candidate.url,
                        published_at=published_at,
                        run_id=self.run_id,
                        raw_path=str(raw_path),
                    )
                    processed_this_run.add(hashed_url)
                    counts["article_urls_new"] += 1
                    if published_at and (not max_seen_published_at or published_at > max_seen_published_at):
                        max_seen_published_at = published_at

                if self._should_stop_paging(oldest_seen_on_source, scan_from):
                    logging.info(
                        "Stopping pagination for %s. oldest_seen=%s < scan_from=%s",
                        source_url,
                        oldest_seen_on_source,
                        scan_from,
                    )
                    break

        self.state.update_checkpoint(SOURCE_NAME, max_seen_published_at)
        return counts

    def close(self) -> None:
        self.state.close()
        self.session.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage 1 ingest scraper for diariodesanse.com")
    parser.add_argument("--sources", nargs="*", default=DEFAULT_SOURCES, help="List/list-page URLs to crawl")
    parser.add_argument("--raw-root", default="data/raw", help="Raw landing root directory")
    parser.add_argument("--db-path", default="data/processed/ingest_state.sqlite3", help="SQLite state DB path")
    parser.add_argument("--overlap-days", type=int, default=2, help="Days to overlap from checkpoint")
    parser.add_argument("--max-pages-per-source", type=int, default=5, help="Cap list pages per source per run")
    parser.add_argument("--max-articles-per-run", type=int, default=150, help="Cap total new articles per run")
    parser.add_argument("--min-sleep-seconds", type=float, default=0.4, help="Min polite delay between requests")
    parser.add_argument("--max-sleep-seconds", type=float, default=1.2, help="Max polite delay between requests")
    parser.add_argument("--timeout-seconds", type=int, default=20, help="HTTP timeout in seconds")
    parser.add_argument(
        "--user-agent",
        default="SanseEventNotifierBot/0.1 (+local learning project; respectful crawl)",
        help="HTTP user agent",
    )
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    return parser


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    scraper: DiarioDeSanseScraper | None = None
    try:
        scraper = DiarioDeSanseScraper(
            sources=args.sources,
            raw_root=Path(args.raw_root),
            db_path=Path(args.db_path),
            overlap_days=args.overlap_days,
            max_pages_per_source=args.max_pages_per_source,
            max_articles_per_run=args.max_articles_per_run,
            min_sleep_seconds=args.min_sleep_seconds,
            max_sleep_seconds=args.max_sleep_seconds,
            user_agent=args.user_agent,
            timeout_seconds=args.timeout_seconds,
        )
        counts = scraper.run()
        logging.info("Run complete: %s", counts)
        print(json.dumps({"run_id": scraper.run_id, "counts": counts}, ensure_ascii=False, indent=2))
        return 0
    except Exception:
        logging.exception("Fatal ingest failure")
        return 1
    finally:
        if scraper is not None:
            scraper.close()


if __name__ == "__main__":
    raise SystemExit(main())
