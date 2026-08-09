from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .models import Movie
from .utils import (
    dt_to_iso,
    normalize_image_url,
    normalize_text,
    parse_int,
    parse_french_date,
    split_list,
    strip_accents,
)

class ParserMixin:

    def extract_release_dates(self, soup: BeautifulSoup, raw_html: str) -> tuple[Optional[datetime], Optional[datetime], str]:
        dvd_date: Optional[datetime] = None
        bluray_date: Optional[datetime] = None
        release_text_parts: list[str] = []

        # Variant 1: explicit labels in plain text.
        combined = re.search(r"Date vente dvd\s+et\s+BLU\s*RAY\s*:\s*([^<\n]+)", raw_html, re.IGNORECASE)
        dvd_only = re.search(r"Date vente dvd\s*:\s*([^<\n]+)", raw_html, re.IGNORECASE)
        br_only = re.search(r"Date vente\s+BLU\s*RAY\s*:\s*([^<\n]+)", raw_html, re.IGNORECASE)

        if combined:
            text = normalize_text(combined.group(1))
            release_text_parts.append(f"DVD + Blu-ray: {text}")
            dt = parse_french_date(text)
            dvd_date = dt or dvd_date
            bluray_date = dt or bluray_date

        if dvd_only:
            text = normalize_text(dvd_only.group(1))
            release_text_parts.append(f"DVD: {text}")
            dt = parse_french_date(text)
            dvd_date = dt or dvd_date

        if br_only:
            text = normalize_text(br_only.group(1))
            release_text_parts.append(f"Blu-ray: {text}")
            dt = parse_french_date(text)
            bluray_date = dt or bluray_date

        # Variant 2: two-column date table currently used on many pages.
        for td in soup.find_all("td"):
            label_text = normalize_text(td.get_text(" ", strip=True)).lower()
            if "vente" not in label_text or ("dvd" not in label_text and "blu" not in label_text):
                continue

            row = td.find_parent("tr")
            if not row:
                continue

            cells = row.find_all("td")
            if len(cells) < 2:
                continue

            labels = [normalize_text(x).lower() for x in cells[0].stripped_strings]
            values = [normalize_text(x) for x in cells[1].stripped_strings]
            if not labels or not values:
                continue

            for i, label in enumerate(labels):
                if i >= len(values):
                    break
                value = normalize_text(values[i]).lstrip(":").strip()
                if not value:
                    continue
                if "inconnue" in value.lower():
                    release_text_parts.append(f"{labels[i]}: {value}")
                    continue

                dt = parse_french_date(value)
                if not dt:
                    continue

                if "dvd" in label:
                    dvd_date = dt
                    release_text_parts.append(f"DVD: {value}")
                if "blu" in label:
                    bluray_date = dt
                    release_text_parts.append(f"Blu-ray: {value}")

        release_text = " | ".join(dict.fromkeys(release_text_parts))
        return dvd_date, bluray_date, release_text

    def extract_title(self, soup: BeautifulSoup) -> str:
        h2_name = soup.select_one('span[itemprop="name"]')
        if h2_name:
            name = normalize_text(h2_name.get_text(" ", strip=True))
            if name:
                return name

        h1 = soup.select_one("h1")
        if h1:
            name = normalize_text(h1.get_text(" ", strip=True))
            if name:
                return name

        title_tag = soup.select_one("title")
        if title_tag:
            text = normalize_text(title_tag.get_text(" ", strip=True))
            text = re.sub(r"\s+-\s+.*$", "", text)
            if text:
                return text

        return ""

    def extract_directors(self, soup: BeautifulSoup) -> list[str]:
        directors = []
        for span in soup.select('span[itemprop="director"] span[itemprop="name"]'):
            value = normalize_text(span.get_text(" ", strip=True))
            if value:
                directors.append(value)
        if directors:
            return list(dict.fromkeys(directors))

        raw = normalize_text(soup.get_text(" ", strip=True))
        match = re.search(r"par\s*:\s*([^\n]+?)\s*(Avec:|Dur[eé]e:|Genre:)", raw, re.IGNORECASE)
        if not match:
            return []
        return split_list(match.group(1))

    def extract_actors(self, soup: BeautifulSoup) -> list[str]:
        actors = []
        for a_tag in soup.select('a[href*="acteur-"]'):
            value = normalize_text(a_tag.get_text(" ", strip=True))
            if value:
                actors.append(value)
        return list(dict.fromkeys(actors))

    def extract_runtime(self, soup: BeautifulSoup, text_blob: str) -> str:
        duration = soup.select_one('span[itemprop="duration"]')
        if duration:
            return normalize_text(duration.get_text(" ", strip=True))

        match = re.search(r"Dur[ée]e\s*:\s*([0-9hmin\s]+)", text_blob, re.IGNORECASE)
        return normalize_text(match.group(1)) if match else ""

    def extract_genres(self, soup: BeautifulSoup, text_blob: str) -> list[str]:
        genre_span = soup.select_one('span[itemprop="genre"]')
        if genre_span:
            return split_list(genre_span.get_text(" ", strip=True))

        match = re.search(r"Genre\s*:\s*([^\n]+)", text_blob, re.IGNORECASE)
        if not match:
            return []
        return split_list(match.group(1))

    def extract_synopsis(self, soup: BeautifulSoup, raw_html: str) -> str:
        story = soup.select_one('div[itemprop="description"]')
        if story:
            text = normalize_text(story.get_text(" ", strip=True))
            if text:
                return text

        match = re.search(
            r"Synopsis(?: usuel)?\s*:\s*(.+?)(?:<br\s*/?>\s*<br\s*/?>|</td>|$)",
            raw_html,
            re.IGNORECASE | re.DOTALL,
        )
        if not match:
            return ""
        return normalize_text(re.sub(r"<[^>]+>", " ", match.group(1)))

    def extract_rating(self, soup: BeautifulSoup) -> tuple[str, Optional[int]]:
        rating = ""
        voters = None

        rating_node = soup.select_one('[itemprop="ratingValue"]')
        if rating_node:
            rating = normalize_text(rating_node.get_text(" ", strip=True)).replace(",", ".")

        voters_node = soup.select_one('[itemprop="ratingCount"]')
        if voters_node:
            voters = parse_int(voters_node.get_text(" ", strip=True))

        return rating, voters

    def extract_poster(self, soup: BeautifulSoup, page_url: str) -> str:
        og = soup.select_one('meta[property="og:image"]')
        if og and og.get("content"):
            return normalize_image_url(urljoin(page_url, og["content"]))

        for img in soup.select("img[src]"):
            src = img.get("src") or ""
            alt = (img.get("alt") or "").lower()
            lowered_src = src.lower()
            if any(token in lowered_src for token in ("imdb-", "allocine", "favicon", "logo/")):
                continue

            width = parse_int(str(img.get("width") or "")) or 0
            height = parse_int(str(img.get("height") or "")) or 0

            if "img/affiches/" in lowered_src:
                return normalize_image_url(urljoin(page_url, src))
            if ("sortie" in alt or "affiche" in lowered_src) and (width >= 180 or height >= 260):
                return normalize_image_url(urljoin(page_url, src))

        fallback = soup.select_one("img[src]")
        if fallback and fallback.get("src"):
            return normalize_image_url(urljoin(page_url, fallback["src"]))

        return "https://www.guide-rapide.com/IMG/divers/favicon.ico"

    def extract_imdb_id(self, soup: BeautifulSoup) -> str:
        imdb_link = soup.select_one('a[href*="imdb.com/title/"]')
        if not imdb_link or not imdb_link.get("href"):
            return ""
        match = re.search(r"/title/(tt\d+)", imdb_link["href"])
        return match.group(1) if match else ""

    def canonical_movie_id(self, guide_rapide_id: int, imdb_id: str) -> str:
        if imdb_id and re.fullmatch(r"tt\d+", imdb_id):
            return imdb_id
        return f"gr-film-{guide_rapide_id}"

    def ensure_canonical_id(self, movie: Movie) -> None:
        movie.id = self.canonical_movie_id(movie.guide_rapide_id, movie.imdb_id)

    def extract_production_countries(self, raw_html: str, text_blob: str) -> list[str]:
        html_match = re.search(
            r"Film\s+r[ée]alis[ée]?\s+en\s*<strong>\d{4}</strong>\s*,\s*(.+?)\s*,\s*par\s*:",
            raw_html,
            re.IGNORECASE | re.DOTALL,
        )
        country_blob = ""
        if html_match:
            country_blob = normalize_text(re.sub(r"<[^>]+>", " ", html_match.group(1)))
        else:
            text_match = re.search(
                r"Film\s+r[ée]alis[ée]?\s+en\s+\d{4}\s*,\s*(.+?)\s*,\s*par\s*:",
                text_blob,
                re.IGNORECASE,
            )
            if text_match:
                country_blob = normalize_text(text_match.group(1))

        if not country_blob:
            return []

        countries = [
            normalize_text(part)
            for part in re.split(r"\s*(?:/|\||;|\+|\set\s|\s&\s)\s*", country_blob, flags=re.IGNORECASE)
            if normalize_text(part)
        ]
        return list(dict.fromkeys(countries))

    def is_french_production(self, movie: Movie) -> bool:
        for country in movie.production_countries:
            normalized = strip_accents(country).lower()
            if "france" in normalized or "francais" in normalized:
                return True
        return False

    def extract_trailer_url(self, raw_html: str) -> str:
        text = raw_html.replace("\\/", "/")
        match = re.search(r"https?://(?:www\.)?(?:youtube\.com|youtu\.be)[^\"'<>\s]+", text)
        if not match:
            return ""
        return match.group(0)

    def parse_movie(self, film_id: int, url: str, html: str) -> Optional[Movie]:
        soup = BeautifulSoup(html, "lxml")
        raw_html = str(soup)
        text_blob = soup.get_text("\n", strip=True)

        title = self.extract_title(soup)
        if not title:
            return None

        year_match = re.search(r"(?:Film r[ée]alis[ée]? en|Film DTV,?)\s*<strong>(\d{4})</strong>", raw_html, re.IGNORECASE)
        if not year_match:
            year_match = re.search(r"\b(19\d{2}|20\d{2})\b", text_blob)
        year = int(year_match.group(1)) if year_match else None

        dvd_dt, bluray_dt, release_text = self.extract_release_dates(soup, raw_html)
        physical_available = bool(dvd_dt or bluray_dt)

        release_type = ""
        if dvd_dt and bluray_dt:
            release_type = "dvd+bluray"
        elif bluray_dt:
            release_type = "bluray"
        elif dvd_dt:
            release_type = "dvd"

        released_dt: Optional[datetime] = None
        if dvd_dt and bluray_dt:
            released_dt = min(dvd_dt, bluray_dt)
        else:
            released_dt = dvd_dt or bluray_dt

        rating, voters = self.extract_rating(soup)
        imdb_id = self.extract_imdb_id(soup)
        if not imdb_id:
            imdb_id = self.lookup_imdb_id_by_title(title, year)

        movie = Movie(
            id=self.canonical_movie_id(film_id, imdb_id=imdb_id),
            source_url=url,
            guide_rapide_id=film_id,
            title=title,
            year=year,
            director=self.extract_directors(soup),
            actors=self.extract_actors(soup),
            runtime=self.extract_runtime(soup, text_blob),
            genres=self.extract_genres(soup, text_blob),
            synopsis=self.extract_synopsis(soup, raw_html),
            rating=rating,
            voters=voters,
            poster=self.extract_poster(soup, url),
            trailer_url=self.extract_trailer_url(raw_html),
            writers=[],
            production_companies=[],
            critic_ratings={},
            content_rating="",
            box_office="",
            awards="",
            metascore="",
            imdb_id=imdb_id,
            production_countries=self.extract_production_countries(raw_html, text_blob),
            dvd_release_date=dt_to_iso(dvd_dt),
            bluray_release_date=dt_to_iso(bluray_dt),
            release_type=release_type,
            release_text=release_text,
            released=dt_to_iso(released_dt),
            physical_available=physical_available,
            checked_at=datetime.now(timezone.utc).isoformat(),
        )

        self.apply_imdb_metadata(movie)
        return movie
