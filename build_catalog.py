from __future__ import annotations

import json
import re
import unicodedata
from collections import deque
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.guide-rapide.com/"
OUTPUT_DIR = Path("site")
CATALOG_DIR = OUTPUT_DIR / "catalog" / "movie"
META_DIR = OUTPUT_DIR / "meta" / "movie"
REQUEST_TIMEOUT = 20
MAX_PAGES = 2500
CURRENT_YEAR = datetime.now().year
START_YEAR = 2000
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; GuideRapideNuvioBot/1.0; "
        "https://github.com/guide-rapide-nuvio)"
    )
}

MONTHS = {
    "janvier": 1,
    "jan": 1,
    "fevrier": 2,
    "février": 2,
    "fev": 2,
    "fév": 2,
    "mars": 3,
    "avril": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "aout": 8,
    "août": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "decembre": 12,
    "décembre": 12,
}

FIELD_PATTERNS = {
    "release_line": re.compile(
        r"Date vente dvd(?: et BLU RAY| et Blu Ray| et blu ray)?\s*:\s*(.+?)(?:<br|\n|$)",
        re.IGNORECASE | re.DOTALL,
    ),
    "blue_release_line": re.compile(
        r"Date vente BLU RAY\s*:\s*(.+?)(?:<br|\n|$)",
        re.IGNORECASE | re.DOTALL,
    ),
    "year": re.compile(r"Ann[éee] de r[ée]alisation\s*:\s*(\d{4})", re.IGNORECASE),
    "director": re.compile(r"Realisateur\s*:\s*(.+?)(?:<br|\n|$)", re.IGNORECASE | re.DOTALL),
    "actors": re.compile(r"Acteurs\s*:\s*(.+?)(?:<br|\n|$)", re.IGNORECASE | re.DOTALL),
    "genres": re.compile(r"Genre\s*:\s*(.+?)(?:<br|\n|$)", re.IGNORECASE | re.DOTALL),
    "score": re.compile(
        r"Note moyenn[ée]e\s*=\s*(\d+[\.,]\d+)\s*/10", re.IGNORECASE
    ),
    "voters": re.compile(r"Nombre de votants cumul[ée]s .*?:\s*([\d\s\u00a0]+)", re.IGNORECASE),
    "synopsis": re.compile(
        r"Synopsis\s*:\s*(.+?)(?:<br\s*/?>\s*<br\s*/?>|Voir la vid[ée]o sur|$)",
        re.IGNORECASE | re.DOTALL,
    ),
}


@dataclass
class FilmEntry:
    id: str
    type: str
    name: str
    poster: str
    posterShape: str = "poster"
    description: str = ""
    releaseInfo: str = ""
    released: str = ""
    genres: list[str] | None = None
    director: list[str] | None = None
    cast: list[str] | None = None
    imdbRating: str = ""
    country: str = "France"
    language: str = "fr"
    sourceUrl: str = ""
    releaseText: str = ""


def normalize_text(value: str) -> str:
    return " ".join(value.split()).strip()


def strip_accents(value: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", value) if not unicodedata.combining(ch)
    )


def parse_french_date(value: str) -> Optional[datetime]:
    cleaned = normalize_text(value).lower()
    cleaned = cleaned.replace("1er", "1")
    cleaned = strip_accents(cleaned)
    parts = cleaned.split()
    if len(parts) < 3:
        return None
    try:
        day = int(parts[0])
        month = MONTHS.get(parts[1])
        year = int(parts[2])
        if not month:
            return None
        return datetime(year, month, day, tzinfo=timezone.utc)
    except Exception:
        return None


def split_names(value: str) -> list[str]:
    value = re.sub(r"<[^>]+>", " ", value)
    value = normalize_text(value)
    parts = re.split(r"\s*,\s*", value)
    return [p for p in (normalize_text(x) for x in parts) if p]


def split_genres(value: str) -> list[str]:
    value = re.sub(r"<[^>]+>", " ", value)
    value = normalize_text(value)
    parts = re.split(r"\s*,\s*", value)
    return [p for p in (normalize_text(x) for x in parts) if p]


def safe_json(obj: object, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class Crawler:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.visited_pages: set[str] = set()
        self.film_urls: set[str] = set()
        self.entries: dict[str, FilmEntry] = {}

    def fetch(self, url: str) -> Optional[str]:
        try:
            resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
        except requests.RequestException:
            return None
        if resp.status_code >= 400:
            return None
        return resp.text

    def build_seed_urls(self) -> list[str]:
        seeds = [BASE_URL]
        for year in range(START_YEAR, CURRENT_YEAR + 1):
            seeds.extend(
                [
                    urljoin(BASE_URL, f"accueil-{year}-tous-note.html"),
                    urljoin(BASE_URL, f"accueil-{year}-note.html"),
                    urljoin(BASE_URL, f"dvd-vente-{year}-tous-note.html"),
                    urljoin(BASE_URL, f"dvd-vente-{year}-tous-note-aeca1a.html"),
                    urljoin(BASE_URL, f"dvd-vente-{year}-fil-date.html"),
                    urljoin(BASE_URL, f"dvd-vente-{year}-futur-date.html"),
                ]
            )
            if year == CURRENT_YEAR:
                for month in range(1, 13):
                    seeds.extend(
                        [
                            urljoin(BASE_URL, f"accueil-{year}-{month:02d}-note.html"),
                            urljoin(BASE_URL, f"accueil-{year}-{month}-note.html"),
                        ]
                    )
        return list(dict.fromkeys(seeds))

    def crawl_archives(self) -> None:
        queue = deque(self.build_seed_urls())
        processed_pages = 0

        while queue and processed_pages < MAX_PAGES:
            url = queue.popleft()
            if url in self.visited_pages:
                continue
            self.visited_pages.add(url)
            html = self.fetch(url)
            processed_pages += 1
            if not html:
                continue

            soup = BeautifulSoup(html, "lxml")
            for a in soup.select('a[href*="/film-"]'):
                href = a.get("href") or ""
                full = urljoin(url, href)
                if self.is_internal(full):
                    self.film_urls.add(full.split("#", 1)[0])

            for a in soup.select("a[href]"):
                href = a.get("href") or ""
                full = urljoin(url, href)
                if self.is_archive_like(full) and full not in self.visited_pages:
                    queue.append(full)

    def is_internal(self, url: str) -> bool:
        parsed = urlparse(url)
        return parsed.netloc.endswith("guide-rapide.com")

    def is_archive_like(self, url: str) -> bool:
        if not self.is_internal(url):
            return False
        path = urlparse(url).path.lower()
        return any(
            token in path
            for token in (
                "accueil-",
                "dvd-vente-",
                "film-",
                "fluxrss",
                "index",
                "palmares",
                "sorties",
                "accueil.html",
                "/",
            )
        )

    def parse_film_page(self, url: str) -> Optional[FilmEntry]:
        html = self.fetch(url)
        if not html:
            return None
        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text("\n", strip=True)
        raw_html = str(soup)

        title = self.extract_title(soup)
        if not title:
            return None

        release_text = self.extract_field(raw_html, FIELD_PATTERNS["release_line"])
        if not release_text:
            release_text = self.extract_field(raw_html, FIELD_PATTERNS["blue_release_line"])
        release_dt = parse_french_date(release_text or "")

        year = self.extract_field(text, FIELD_PATTERNS["year"]) or ""
        director = split_names(self.extract_field(raw_html, FIELD_PATTERNS["director"]) or "")
        actors = split_names(self.extract_field(raw_html, FIELD_PATTERNS["actors"]) or "")
        genres = split_genres(self.extract_field(raw_html, FIELD_PATTERNS["genres"]) or "")
        synopsis = normalize_text(self.extract_field(raw_html, FIELD_PATTERNS["synopsis"]) or "")
        score = self.extract_field(text, FIELD_PATTERNS["score"]) or ""
        poster = self.extract_poster(soup, url)

        if not poster:
            poster = "https://www.guide-rapide.com/favicon.ico"

        film_id = self.make_id(url)
        description_bits = []
        if release_text:
            description_bits.append(f"Release: {release_text}")
        if year:
            description_bits.append(f"Year: {year}")
        if director:
            description_bits.append("Director: " + ", ".join(director))
        if genres:
            description_bits.append("Genres: " + ", ".join(genres))
        if synopsis:
            description_bits.append(synopsis)
        description = "\n\n".join(description_bits).strip()

        entry = FilmEntry(
            id=film_id,
            type="movie",
            name=normalize_text(title),
            poster=poster,
            description=description,
            releaseInfo=year,
            released=release_dt.isoformat().replace("+00:00", "Z") if release_dt else "",
            genres=genres or [],
            director=director or [],
            cast=actors or [],
            imdbRating=score,
            sourceUrl=url,
            releaseText=release_text or "",
        )
        return entry

    def extract_title(self, soup: BeautifulSoup) -> str:
        for selector in ("h1", "title"):
            el = soup.select_one(selector)
            if el:
                value = normalize_text(el.get_text(" ", strip=True))
                if value:
                    return re.split(r"\s+[-|]\s+", value)[0].strip()
        return ""

    def extract_field(self, text: str, pattern: re.Pattern[str]) -> str:
        match = pattern.search(text)
        if not match:
            return ""
        return normalize_text(re.sub(r"<[^>]+>", " ", match.group(1)))

    def extract_poster(self, soup: BeautifulSoup, page_url: str) -> str:
        og = soup.select_one('meta[property="og:image"]')
        if og and og.get("content"):
            return urljoin(page_url, og["content"])
        link = soup.select_one('link[rel="image_src"]')
        if link and link.get("href"):
            return urljoin(page_url, link["href"])
        img = soup.select_one("img[src]")
        if img and img.get("src"):
            return urljoin(page_url, img["src"])
        return ""

    def make_id(self, url: str) -> str:
        path = urlparse(url).path.rstrip("/")
        leaf = path.split("/")[-1]
        leaf = leaf.replace(".html", "")
        return f"gr:{leaf}"

    def build(self) -> None:
        self.crawl_archives()

        for film_url in sorted(self.film_urls):
            if len(self.entries) >= MAX_PAGES:
                break
            entry = self.parse_film_page(film_url)
            if entry:
                self.entries[entry.id] = entry

        items = list(self.entries.values())
        items.sort(key=lambda item: item.released or "", reverse=True)

        all_metas = [self.to_meta_preview(item) for item in items]
        dvd_metas = [meta for meta in all_metas if self.is_dvd_item(meta)]

        safe_json({"metas": all_metas}, CATALOG_DIR / "all-releases.json")
        safe_json({"metas": dvd_metas}, CATALOG_DIR / "dvd-france.json")

        for item in items:
            safe_json({"meta": self.to_meta(item)}, META_DIR / f"{item.id.replace(':', '_')}.json")

        self.write_index(items)
        self.write_manifest()

    def is_dvd_item(self, meta: dict) -> bool:
        text = " ".join(
            str(meta.get(k, "")) for k in ("name", "description", "releaseInfo")
        ).lower()
        return "dvd" in text

    def to_meta_preview(self, item: FilmEntry) -> dict:
        meta = {
            "id": item.id,
            "type": item.type,
            "name": item.name,
            "poster": item.poster,
        }
        if item.genres:
            meta["genres"] = item.genres
        if item.releaseInfo:
            meta["releaseInfo"] = item.releaseInfo
        if item.description:
            meta["description"] = item.description[:500]
        if item.imdbRating:
            meta["imdbRating"] = item.imdbRating
        return meta

    def to_meta(self, item: FilmEntry) -> dict:
        meta = self.to_meta_preview(item)
        if item.poster:
            meta["poster"] = item.poster
        if item.director:
            meta["director"] = item.director
        if item.cast:
            meta["cast"] = item.cast
        if item.released:
            meta["released"] = item.released
        meta["country"] = item.country
        meta["language"] = item.language
        meta["background"] = item.poster
        meta["logo"] = "https://www.guide-rapide.com/favicon.ico"
        meta["links"] = [{"name": "Source", "category": "source", "url": item.sourceUrl}]
        return meta

    def write_manifest(self) -> None:
        manifest = {
            "id": "org.guiderapide.nuvio",
            "version": "1.0.0",
            "name": "Guide Rapide",
            "description": "Guide Rapide French DVD and Blu-ray release catalog",
            "logo": "https://www.guide-rapide.com/favicon.ico",
            "background": "https://www.guide-rapide.com/favicon.ico",
            "resources": [
                "catalog",
                {
                    "name": "meta",
                    "types": ["movie"],
                    "idPrefixes": ["gr:"]
                }
            ],
            "types": ["movie"],
            "catalogs": [
                {"type": "movie", "id": "all-releases", "name": "Guide Rapide - All releases"},
                {"type": "movie", "id": "dvd-france", "name": "Guide Rapide - DVD France"},
            ],
        }
        safe_json(manifest, OUTPUT_DIR / "manifest.json")

    def write_index(self, items: list[FilmEntry]) -> None:
        html = [
            "<!doctype html>",
            "<html lang=\"en\">",
            "<head>",
            "<meta charset=\"utf-8\">",
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
            "<title>Guide Rapide addon</title>",
            "<style>body{font-family:system-ui,sans-serif;max-width:900px;margin:40px auto;padding:0 16px;line-height:1.5}code{background:#f3f3f3;padding:2px 5px;border-radius:4px}</style>",
            "</head>",
            "<body>",
            "<h1>Guide Rapide addon</h1>",
            "<p>Install this addon in Nuvio using the manifest at <code>/manifest.json</code>.</p>",
            f"<p>Items collected: <strong>{len(items)}</strong></p>",
            "<ul>",
            "<li><a href=\"manifest.json\">manifest.json</a></li>",
            "<li><a href=\"catalog/movie/all-releases.json\">all-releases catalog</a></li>",
            "<li><a href=\"catalog/movie/dvd-france.json\">dvd-france catalog</a></li>",
            "</ul>",
            "</body></html>",
        ]
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUTPUT_DIR / "index.html").write_text("\n".join(html), encoding="utf-8")


def main() -> int:
    crawler = Crawler()
    crawler.build()
    print(f"Collected {len(crawler.entries)} items")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
