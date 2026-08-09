from __future__ import annotations

import json
import re
from dataclasses import asdict
from typing import Optional
from urllib.parse import quote

from bs4 import BeautifulSoup

from .config import IMDB_SUGGESTION_API, OMDB_API_URL, TMDB_API_URL
from .models import ImdbMetadata
from .utils import (
    normalize_image_url,
    normalize_text,
    parse_int,
    split_list,
    strip_accents,
)

class MetadataMixin:

    def merge_metadata(self, primary: ImdbMetadata, secondary: ImdbMetadata) -> ImdbMetadata:
        return ImdbMetadata(
            title=primary.title or secondary.title,
            year=primary.year if primary.year is not None else secondary.year,
            director=primary.director or secondary.director,
            actors=primary.actors or secondary.actors,
            poster=primary.poster or secondary.poster,
            description=primary.description or secondary.description,
            genres=primary.genres or secondary.genres,
            rating=primary.rating or secondary.rating,
            voters=primary.voters if primary.voters is not None else secondary.voters,
            runtime=primary.runtime or secondary.runtime,
            trailer_url=primary.trailer_url or secondary.trailer_url,
            writers=primary.writers or secondary.writers,
            production_companies=primary.production_companies or secondary.production_companies,
            critic_ratings=primary.critic_ratings or secondary.critic_ratings,
            content_rating=primary.content_rating or secondary.content_rating,
            box_office=primary.box_office or secondary.box_office,
            awards=primary.awards or secondary.awards,
            metascore=primary.metascore or secondary.metascore,
        )

    def build_omdb_key_pool(self) -> list[str]:
        keys: list[str] = []
        if self.config.omdb_api_key:
            keys.append(self.config.omdb_api_key)
        if self.config.omdb_api_keys_raw:
            for raw in self.config.omdb_api_keys_raw.split(","):
                key = normalize_text(raw)
                if key:
                    keys.append(key)

        # Preserve order while dropping duplicates.
        return list(dict.fromkeys(keys))

    def fetch_omdb_json_with_key_fallback(self, params: dict[str, str]) -> Optional[dict]:
        if not self.omdb_api_keys:
            return None

        for key in self.omdb_api_keys:
            payload = self.fetch_json(OMDB_API_URL, params={**params, "apikey": key})
            if not payload:
                continue

            # Switch to next key when current key is invalid or daily quota is exhausted.
            response_ok = normalize_text(str(payload.get("Response") or "")).lower() == "true"
            if response_ok:
                return payload

            error_text = normalize_text(str(payload.get("Error") or "")).lower()
            if "invalid api key" in error_text or "request limit reached" in error_text:
                continue

            # For true content misses (movie not found), no need to try every key.
            return payload

        return None

    def should_use_omdb(self) -> bool:
        if self.metadata_provider == "imdb":
            return False
        return bool(self.omdb_api_keys)

    def should_use_tmdb(self) -> bool:
        if self.metadata_provider == "imdb":
            return False
        if self.metadata_provider == "omdb":
            return False
        return bool(self.config.tmdb_api_key)

    def fetch_tmdb_json(
        self,
        path: str,
        params: Optional[dict[str, str]] = None,
        include_default_language: bool = True,
    ) -> Optional[dict]:
        if not self.should_use_tmdb():
            return None

        query = {"api_key": self.config.tmdb_api_key}
        if include_default_language:
            query["language"] = "fr-FR"
        if params:
            query.update(params)
        return self.fetch_json(f"{TMDB_API_URL}{path}", params=query)

    def pick_tmdb_trailer_url(self, results: list[dict]) -> str:
        ranked: list[tuple[int, str]] = []
        for video in results:
            if not isinstance(video, dict):
                continue
            if normalize_text(str(video.get("site") or "")).lower() != "youtube":
                continue

            key = normalize_text(str(video.get("key") or ""))
            if not key:
                continue

            kind = normalize_text(str(video.get("type") or "")).lower()
            if kind not in {"trailer", "teaser"}:
                continue

            name = normalize_text(str(video.get("name") or "")).lower()
            official = bool(video.get("official"))
            score = 0
            if kind == "trailer":
                score += 100
            if official:
                score += 30
            if "official" in name:
                score += 10
            if "trailer" in name:
                score += 5

            ranked.append((score, f"https://www.youtube.com/watch?v={key}"))

        if not ranked:
            return ""
        ranked.sort(key=lambda item: item[0], reverse=True)
        return ranked[0][1]

    def resolve_tmdb_trailer_url(self, movie_id: int, seed_results: Optional[list[dict]] = None) -> str:
        # Try trailers in preferred language first (fr-FR), then widen to en-US and no language.
        if seed_results:
            url = self.pick_tmdb_trailer_url(seed_results)
            if url:
                return url

        for lang in ("en-US", ""):
            params: dict[str, str] = {}
            include_default_language = False
            if lang:
                params["language"] = lang
            videos_payload = self.fetch_tmdb_json(
                f"/movie/{movie_id}/videos",
                params=params,
                include_default_language=include_default_language,
            )
            if not isinstance(videos_payload, dict):
                continue
            results = videos_payload.get("results")
            if not isinstance(results, list):
                continue
            url = self.pick_tmdb_trailer_url(results)
            if url:
                return url

        return ""

    def youtube_search_trailer_url(self, imdb_id: str, title: str) -> str:
        query_parts = [imdb_id, title, "official trailer"]
        query = normalize_text(" ".join(x for x in query_parts if x))
        if not query:
            return ""
        return f"https://www.youtube.com/results?search_query={quote(query)}"

    def tmdb_poster_url(self, poster_path: str) -> str:
        normalized = normalize_text(poster_path)
        if not normalized:
            return ""
        if normalized.startswith("http://") or normalized.startswith("https://"):
            return normalized
        return f"https://image.tmdb.org/t/p/w780{normalized}"

    def tmdb_minutes_to_runtime(self, runtime_minutes: int) -> str:
        if runtime_minutes <= 0:
            return ""
        hours, mins = divmod(runtime_minutes, 60)
        if hours and mins:
            return f"{hours}h{mins:02d}"
        if hours:
            return f"{hours}h"
        return f"{mins}min"

    def tmdb_payload_to_metadata(self, details: dict) -> ImdbMetadata:
        title = normalize_text(str(details.get("title") or details.get("name") or ""))

        year = None
        release_date = normalize_text(str(details.get("release_date") or ""))
        year_match = re.match(r"(\d{4})", release_date)
        if year_match:
            year = int(year_match.group(1))

        directors: list[str] = []
        actors: list[str] = []

        credits = details.get("credits")
        if isinstance(credits, dict):
            crew = credits.get("crew")
            if isinstance(crew, list):
                for item in crew:
                    if not isinstance(item, dict):
                        continue
                    if str(item.get("job") or "").lower() != "director":
                        continue
                    name = normalize_text(str(item.get("name") or ""))
                    if name:
                        directors.append(name)

            cast = credits.get("cast")
            if isinstance(cast, list):
                for item in cast[:10]:
                    if not isinstance(item, dict):
                        continue
                    name = normalize_text(str(item.get("name") or ""))
                    if name:
                        actors.append(name)

        genres: list[str] = []
        genres_raw = details.get("genres")
        if isinstance(genres_raw, list):
            for item in genres_raw:
                if not isinstance(item, dict):
                    continue
                name = normalize_text(str(item.get("name") or ""))
                if name:
                    genres.append(name)

        production_companies: list[str] = []
        companies_raw = details.get("production_companies")
        if isinstance(companies_raw, list):
            for item in companies_raw:
                if not isinstance(item, dict):
                    continue
                name = normalize_text(str(item.get("name") or ""))
                if name:
                    production_companies.append(name)

        trailer_url = normalize_text(str(details.get("_resolved_trailer_url") or ""))
        if not trailer_url:
            videos = details.get("videos")
            if isinstance(videos, dict):
                results = videos.get("results")
                if isinstance(results, list):
                    trailer_url = self.pick_tmdb_trailer_url(results)

        rating = ""
        vote_average = details.get("vote_average")
        if isinstance(vote_average, (int, float)) and vote_average > 0:
            rating = f"{vote_average:.1f}".rstrip("0").rstrip(".")

        ratings_map: dict[str, str] = {}
        if rating:
            ratings_map["TMDB"] = f"{rating}/10"

        voters = None
        vote_count = details.get("vote_count")
        if isinstance(vote_count, int) and vote_count > 0:
            voters = vote_count

        runtime = ""
        runtime_raw = details.get("runtime")
        if isinstance(runtime_raw, int):
            runtime = self.tmdb_minutes_to_runtime(runtime_raw)

        poster = self.tmdb_poster_url(str(details.get("poster_path") or ""))

        description = normalize_text(str(details.get("overview") or ""))

        return ImdbMetadata(
            title=title,
            year=year,
            director=list(dict.fromkeys(directors)) or None,
            actors=list(dict.fromkeys(actors)) or None,
            poster=poster,
            description=description,
            genres=list(dict.fromkeys(genres)) or None,
            rating=rating,
            voters=voters,
            runtime=runtime,
            trailer_url=trailer_url,
            writers=None,
            production_companies=list(dict.fromkeys(production_companies)) or None,
            critic_ratings=ratings_map or None,
            content_rating="",
            box_office="",
            awards="",
            metascore="",
        )

    def omdb_payload_to_metadata(self, payload: dict) -> ImdbMetadata:
        title = normalize_text(str(payload.get("Title") or ""))

        year = None
        year_raw = normalize_text(str(payload.get("Year") or ""))
        year_match = re.match(r"(\d{4})", year_raw)
        if year_match:
            year = int(year_match.group(1))

        directors = split_list(str(payload.get("Director") or "").replace("N/A", ""))
        actors = split_list(str(payload.get("Actors") or "").replace("N/A", ""))
        writers = split_list(str(payload.get("Writer") or "").replace("N/A", ""))
        production_companies = split_list(str(payload.get("Production") or "").replace("N/A", ""))

        genres = split_list(str(payload.get("Genre") or "").replace("N/A", ""))

        rating = normalize_text(str(payload.get("imdbRating") or ""))
        if rating == "N/A":
            rating = ""

        voters_raw = normalize_text(str(payload.get("imdbVotes") or ""))
        voters = parse_int(voters_raw) if voters_raw and voters_raw != "N/A" else None

        runtime = normalize_text(str(payload.get("Runtime") or ""))
        if runtime == "N/A":
            runtime = ""

        poster = normalize_image_url(str(payload.get("Poster") or "").strip())
        if poster == "N/A":
            poster = ""

        description = normalize_text(str(payload.get("Plot") or ""))
        if description == "N/A":
            description = ""

        content_rating = normalize_text(str(payload.get("Rated") or ""))
        if content_rating == "N/A":
            content_rating = ""

        box_office = normalize_text(str(payload.get("BoxOffice") or ""))
        if box_office == "N/A":
            box_office = ""

        ratings_map: dict[str, str] = {}
        ratings_raw = payload.get("Ratings")
        if isinstance(ratings_raw, list):
            for item in ratings_raw:
                if not isinstance(item, dict):
                    continue
                source = normalize_text(str(item.get("Source") or ""))
                value = normalize_text(str(item.get("Value") or ""))
                if source and value:
                    ratings_map[source] = value

        awards = normalize_text(str(payload.get("Awards") or ""))
        if awards == "N/A":
            awards = ""

        metascore = normalize_text(str(payload.get("Metascore") or ""))
        if metascore == "N/A":
            metascore = ""

        return ImdbMetadata(
            title=title,
            year=year,
            director=directors or None,
            actors=actors or None,
            poster=poster,
            description=description,
            genres=genres or None,
            rating=rating,
            voters=voters,
            runtime=runtime,
            trailer_url="",
            writers=writers or None,
            production_companies=production_companies or None,
            critic_ratings=ratings_map or None,
            content_rating=content_rating,
            box_office=box_office,
            awards=awards,
            metascore=metascore,
        )

    def lookup_imdb_id_via_omdb(self, title: str, year: Optional[int]) -> str:
        if not self.should_use_omdb():
            return ""

        cleaned_title = normalize_text(title)
        if not cleaned_title:
            return ""

        cache_key = f"omdb_search::{cleaned_title.lower()}::{year or ''}"
        cached = self.imdb_cache.get(cache_key)
        if isinstance(cached, dict):
            imdb_id = str(cached.get("imdb_id", ""))
            return imdb_id if re.fullmatch(r"tt\d+", imdb_id) else ""

        params = {"t": cleaned_title, "plot": "short"}
        if year is not None:
            params["y"] = str(year)

        payload = self.fetch_omdb_json_with_key_fallback(params=params)
        if not payload or str(payload.get("Response", "")).lower() != "true":
            self.imdb_cache[cache_key] = {"imdb_id": ""}
            return ""

        imdb_id = normalize_text(str(payload.get("imdbID") or ""))
        if not re.fullmatch(r"tt\d+", imdb_id):
            self.imdb_cache[cache_key] = {"imdb_id": ""}
            return ""

        self.imdb_cache[cache_key] = {"imdb_id": imdb_id}
        self.imdb_cache[imdb_id] = asdict(self.omdb_payload_to_metadata(payload))
        return imdb_id

    def lookup_imdb_id_via_tmdb(self, title: str, year: Optional[int]) -> str:
        if not self.should_use_tmdb():
            return ""

        cleaned_title = normalize_text(title)
        if not cleaned_title:
            return ""

        cache_key = f"tmdb_search::{cleaned_title.lower()}::{year or ''}"
        cached = self.imdb_cache.get(cache_key)
        if isinstance(cached, dict):
            imdb_id = str(cached.get("imdb_id", ""))
            return imdb_id if re.fullmatch(r"tt\d+", imdb_id) else ""

        params = {"query": cleaned_title, "include_adult": "false"}
        if year is not None:
            params["year"] = str(year)

        search_payload = self.fetch_tmdb_json("/search/movie", params=params)
        if not search_payload:
            self.imdb_cache[cache_key] = {"imdb_id": ""}
            return ""

        results = search_payload.get("results")
        if not isinstance(results, list):
            self.imdb_cache[cache_key] = {"imdb_id": ""}
            return ""

        query_norm = strip_accents(cleaned_title).lower()
        for item in results[:6]:
            if not isinstance(item, dict):
                continue

            movie_id = item.get("id")
            if not isinstance(movie_id, int):
                continue

            candidate_title = normalize_text(str(item.get("title") or ""))
            candidate_norm = strip_accents(candidate_title).lower()
            if candidate_norm and query_norm not in candidate_norm and candidate_norm not in query_norm:
                continue

            if year is not None:
                release_date = normalize_text(str(item.get("release_date") or ""))
                year_match = re.match(r"(\d{4})", release_date)
                if year_match and abs(int(year_match.group(1)) - year) > 1:
                    continue

            external = self.fetch_tmdb_json(f"/movie/{movie_id}/external_ids")
            if not isinstance(external, dict):
                continue
            imdb_id = normalize_text(str(external.get("imdb_id") or ""))
            if not re.fullmatch(r"tt\d+", imdb_id):
                continue

            self.imdb_cache[cache_key] = {"imdb_id": imdb_id}
            return imdb_id

        self.imdb_cache[cache_key] = {"imdb_id": ""}
        return ""

    def fetch_omdb_metadata_by_imdb_id(self, imdb_id: str) -> ImdbMetadata:
        if not self.should_use_omdb() or not re.fullmatch(r"tt\d+", imdb_id):
            return ImdbMetadata()

        params = {"i": imdb_id, "plot": "full"}
        payload = self.fetch_omdb_json_with_key_fallback(params=params)
        if not payload or str(payload.get("Response", "")).lower() != "true":
            return ImdbMetadata()

        meta = self.omdb_payload_to_metadata(payload)
        self.imdb_cache[imdb_id] = asdict(meta)
        return meta

    def fetch_tmdb_metadata_by_imdb_id(self, imdb_id: str) -> ImdbMetadata:
        if not self.should_use_tmdb() or not re.fullmatch(r"tt\d+", imdb_id):
            return ImdbMetadata()

        find_payload = self.fetch_tmdb_json(
            f"/find/{imdb_id}",
            params={"external_source": "imdb_id"},
        )
        if not find_payload:
            return ImdbMetadata()

        movie_results = find_payload.get("movie_results")
        if not isinstance(movie_results, list) or not movie_results:
            return ImdbMetadata()

        first = movie_results[0]
        if not isinstance(first, dict):
            return ImdbMetadata()

        movie_id = first.get("id")
        if not isinstance(movie_id, int):
            return ImdbMetadata()

        details = self.fetch_tmdb_json(
            f"/movie/{movie_id}",
            params={"append_to_response": "credits,videos"},
        )
        if not isinstance(details, dict):
            return ImdbMetadata()

        seed_results = None
        videos = details.get("videos")
        if isinstance(videos, dict):
            maybe_results = videos.get("results")
            if isinstance(maybe_results, list):
                seed_results = maybe_results

        trailer_url = self.resolve_tmdb_trailer_url(movie_id, seed_results=seed_results)
        if trailer_url:
            details["_resolved_trailer_url"] = trailer_url

        meta = self.tmdb_payload_to_metadata(details)
        if not meta.trailer_url:
            meta.trailer_url = self.youtube_search_trailer_url(imdb_id, meta.title)
        self.imdb_cache[imdb_id] = asdict(meta)
        return meta

    def lookup_imdb_id_by_title(self, title: str, year: Optional[int]) -> str:
        cleaned_title = normalize_text(title)
        if not cleaned_title:
            return ""

        omdb_imdb_id = self.lookup_imdb_id_via_omdb(cleaned_title, year)
        if omdb_imdb_id:
            return omdb_imdb_id

        tmdb_imdb_id = self.lookup_imdb_id_via_tmdb(cleaned_title, year)
        if tmdb_imdb_id:
            return tmdb_imdb_id

        if not self.config.enable_imdb_suggestion_fallback:
            return ""

        cache_key = f"search::{cleaned_title.lower()}::{year or ''}"
        cached = self.imdb_cache.get(cache_key)
        if isinstance(cached, dict):
            imdb_id = str(cached.get("imdb_id", ""))
            return imdb_id if re.fullmatch(r"tt\d+", imdb_id) else ""

        normalized_query = strip_accents(cleaned_title).lower()
        safe_query = re.sub(r"[^a-z0-9 ]+", " ", normalized_query)
        safe_query = normalize_text(safe_query)
        if not safe_query:
            self.imdb_cache[cache_key] = {"imdb_id": ""}
            return ""

        first = safe_query[0]
        encoded_query = quote(safe_query)
        url = f"{IMDB_SUGGESTION_API}/{first}/{encoded_query}.json"
        raw = self.fetch_url(url)
        if not raw:
            self.imdb_cache[cache_key] = {"imdb_id": ""}
            return ""

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            self.imdb_cache[cache_key] = {"imdb_id": ""}
            return ""

        candidates = payload.get("d") if isinstance(payload, dict) else None
        if not isinstance(candidates, list):
            self.imdb_cache[cache_key] = {"imdb_id": ""}
            return ""

        best_id = ""
        query_norm = strip_accents(cleaned_title).lower()

        for item in candidates:
            if not isinstance(item, dict):
                continue
            imdb_id = str(item.get("id", ""))
            if not re.fullmatch(r"tt\d+", imdb_id):
                continue

            item_title = normalize_text(str(item.get("l", "")))
            if not item_title:
                continue

            item_norm = strip_accents(item_title).lower()
            if query_norm not in item_norm and item_norm not in query_norm:
                continue

            item_year = item.get("y")
            if isinstance(item_year, int) and year is not None and abs(item_year - year) > 1:
                continue

            best_id = imdb_id
            break

        self.imdb_cache[cache_key] = {"imdb_id": best_id}
        return best_id

    def fetch_imdb_metadata(self, imdb_id: str, allow_backfill: bool = True) -> ImdbMetadata:
        if not imdb_id:
            return ImdbMetadata()

        cached = self.imdb_cache.get(imdb_id)
        if isinstance(cached, dict):
            try:
                cached_meta = ImdbMetadata(**cached)
                if not allow_backfill:
                    return cached_meta
                needs_tmdb_backfill = not normalize_text(cached_meta.trailer_url) or not cached_meta.production_companies
                if not needs_tmdb_backfill:
                    return cached_meta

                tmdb_meta = self.fetch_tmdb_metadata_by_imdb_id(imdb_id)
                merged_cached = self.merge_metadata(cached_meta, tmdb_meta)
                self.imdb_cache[imdb_id] = asdict(merged_cached)
                return merged_cached
            except TypeError:
                pass

        omdb_meta = self.fetch_omdb_metadata_by_imdb_id(imdb_id)
        tmdb_meta = self.fetch_tmdb_metadata_by_imdb_id(imdb_id)

        merged = self.merge_metadata(omdb_meta, tmdb_meta)

        if merged.title or merged.poster or merged.description or merged.trailer_url:
            self.imdb_cache[imdb_id] = asdict(merged)
            return merged

        if not self.config.enable_imdb_html_fallback:
            return ImdbMetadata()

        url = f"https://www.imdb.com/title/{imdb_id}/"
        html = self.fetch_url(url)
        if not html:
            return ImdbMetadata()

        soup = BeautifulSoup(html, "lxml")
        data = {}
        for tag in soup.select('script[type="application/ld+json"]'):
            text = tag.string or tag.get_text("", strip=True)
            if not text:
                continue
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                continue

            if isinstance(parsed, dict) and parsed.get("@type") == "Movie":
                data = parsed
                break
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict) and item.get("@type") == "Movie":
                        data = item
                        break
                if data:
                    break

        if not data:
            return ImdbMetadata()

        rating = ""
        voters = None
        aggregate = data.get("aggregateRating")
        if isinstance(aggregate, dict):
            rating_raw = aggregate.get("ratingValue")
            if isinstance(rating_raw, (int, float, str)):
                rating = str(rating_raw)
            voters_raw = aggregate.get("ratingCount")
            if isinstance(voters_raw, (int, float, str)):
                voters = parse_int(str(voters_raw))

        runtime = ""
        duration_raw = data.get("duration")
        if isinstance(duration_raw, str):
            m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?", duration_raw)
            if m:
                h = int(m.group(1) or 0)
                mins = int(m.group(2) or 0)
                if h and mins:
                    runtime = f"{h}h{mins:02d}"
                elif h:
                    runtime = f"{h}h"
                elif mins:
                    runtime = f"{mins}min"

        genres: list[str] = []
        genre_raw = data.get("genre")
        if isinstance(genre_raw, str):
            genres = [normalize_text(genre_raw)]
        elif isinstance(genre_raw, list):
            genres = [normalize_text(str(x)) for x in genre_raw if normalize_text(str(x))]

        directors: list[str] = []
        director_raw = data.get("director")
        if isinstance(director_raw, dict):
            name = normalize_text(str(director_raw.get("name") or ""))
            if name:
                directors.append(name)
        elif isinstance(director_raw, list):
            for item in director_raw:
                if not isinstance(item, dict):
                    continue
                name = normalize_text(str(item.get("name") or ""))
                if name:
                    directors.append(name)

        actors: list[str] = []
        actor_raw = data.get("actor")
        if isinstance(actor_raw, dict):
            name = normalize_text(str(actor_raw.get("name") or ""))
            if name:
                actors.append(name)
        elif isinstance(actor_raw, list):
            for item in actor_raw:
                if not isinstance(item, dict):
                    continue
                name = normalize_text(str(item.get("name") or ""))
                if name:
                    actors.append(name)

        year = None
        date_published = normalize_text(str(data.get("datePublished") or ""))
        year_match = re.match(r"(\d{4})", date_published)
        if year_match:
            year = int(year_match.group(1))

        trailer_url = ""
        trailer_raw = data.get("trailer")
        if isinstance(trailer_raw, dict):
            trailer_url = normalize_text(
                str(trailer_raw.get("embedUrl") or trailer_raw.get("url") or "")
            )

        writers: list[str] = []
        writer_raw = data.get("creator")
        if isinstance(writer_raw, dict):
            name = normalize_text(str(writer_raw.get("name") or ""))
            if name:
                writers.append(name)
        elif isinstance(writer_raw, list):
            for item in writer_raw:
                if not isinstance(item, dict):
                    continue
                name = normalize_text(str(item.get("name") or ""))
                if name:
                    writers.append(name)

        meta = ImdbMetadata(
            title=normalize_text(str(data.get("name") or "")),
            year=year,
            director=list(dict.fromkeys(directors)) or None,
            actors=list(dict.fromkeys(actors)) or None,
            poster=normalize_image_url(str(data.get("image") or "").strip()),
            description=normalize_text(str(data.get("description") or "")),
            genres=genres,
            rating=rating,
            voters=voters,
            runtime=runtime,
            trailer_url=trailer_url,
            writers=list(dict.fromkeys(writers)) or None,
            production_companies=None,
            critic_ratings=None,
            content_rating="",
            box_office="",
            awards="",
            metascore="",
        )
        self.imdb_cache[imdb_id] = asdict(meta)
        return meta

    def has_cached_imdb_metadata(self, imdb_id: str) -> bool:
        if not imdb_id:
            return False
        cached = self.imdb_cache.get(imdb_id)
        return isinstance(cached, dict)

    def apply_imdb_metadata(self, movie: Movie, allow_network: bool = True) -> bool:
        if not movie.imdb_id:
            return False

        if not allow_network and not self.has_cached_imdb_metadata(movie.imdb_id):
            return False

        imdb = self.fetch_imdb_metadata(movie.imdb_id, allow_backfill=allow_network)
        changed = False

        if imdb.title and movie.title != imdb.title:
            movie.title = imdb.title
            changed = True
        if imdb.year is not None and movie.year != imdb.year:
            movie.year = imdb.year
            changed = True
        if imdb.director and movie.director != imdb.director:
            movie.director = imdb.director
            changed = True
        if imdb.actors and movie.actors != imdb.actors:
            movie.actors = imdb.actors
            changed = True
        if imdb.poster and movie.poster != imdb.poster:
            movie.poster = imdb.poster
            changed = True
        if imdb.description and movie.synopsis != imdb.description:
            movie.synopsis = imdb.description
            changed = True
        if imdb.genres and movie.genres != imdb.genres:
            movie.genres = imdb.genres
            changed = True
        if imdb.rating and movie.rating != imdb.rating:
            movie.rating = imdb.rating
            changed = True
        if imdb.voters is not None and movie.voters != imdb.voters:
            movie.voters = imdb.voters
            changed = True
        if imdb.runtime and movie.runtime != imdb.runtime:
            movie.runtime = imdb.runtime
            changed = True
        if imdb.trailer_url and movie.trailer_url != imdb.trailer_url:
            movie.trailer_url = imdb.trailer_url
            changed = True
        if imdb.writers and movie.writers != imdb.writers:
            movie.writers = imdb.writers
            changed = True
        if imdb.production_companies and movie.production_companies != imdb.production_companies:
            movie.production_companies = imdb.production_companies
            changed = True
        if imdb.critic_ratings and movie.critic_ratings != imdb.critic_ratings:
            movie.critic_ratings = imdb.critic_ratings
            changed = True
        if imdb.content_rating and movie.content_rating != imdb.content_rating:
            movie.content_rating = imdb.content_rating
            changed = True
        if imdb.box_office and movie.box_office != imdb.box_office:
            movie.box_office = imdb.box_office
            changed = True
        if imdb.awards and movie.awards != imdb.awards:
            movie.awards = imdb.awards
            changed = True
        if imdb.metascore and movie.metascore != imdb.metascore:
            movie.metascore = imdb.metascore
            changed = True

        return changed

    def should_refresh_poster_from_imdb(self, movie: Movie) -> bool:
        if not movie.imdb_id:
            return False
        if not movie.poster:
            return True
        lowered = movie.poster.lower()
        if lowered.startswith("http://"):
            return True
        if "guide-rapide.com" in lowered:
            return True
        return False

    def should_refresh_trailer_from_api(self, movie: Movie) -> bool:
        if not movie.imdb_id:
            return False
        return not normalize_text(movie.trailer_url)

    def should_backfill_metadata(self, movie: Movie) -> bool:
        if not movie.imdb_id:
            return False

        if self.metadata_backfill_mode == "off":
            return False

        if self.metadata_backfill_mode == "deep":
            return True

        # smart mode: refresh only when key high-value fields are missing.
        needs_poster = self.should_refresh_poster_from_imdb(movie)
        needs_trailer = self.should_refresh_trailer_from_api(movie)
        return needs_poster or needs_trailer

    def refresh_catalog_posters(self, catalogs: dict[str, list[Movie]]) -> tuple[int, int]:
        refreshed = 0
        seen_ids: set[str] = set()

        if self.metadata_backfill_mode == "off":
            return refreshed, self.metadata_backfill_attempts

        for entries in catalogs.values():
            for movie in entries:
                if refreshed >= self.config.max_imdb_poster_refresh_per_run:
                    return refreshed, self.metadata_backfill_attempts
                if self.metadata_api_requests >= self.config.max_metadata_api_lookups_per_run:
                    return refreshed, self.metadata_backfill_attempts
                if movie.id in seen_ids:
                    continue
                seen_ids.add(movie.id)

                movie.poster = normalize_image_url(movie.poster)
                if not self.should_backfill_metadata(movie):
                    continue

                self.metadata_backfill_attempts += 1
                changed = self.apply_imdb_metadata(movie, allow_network=True)
                if changed:
                    self.write_cached_movie(movie)
                    refreshed += 1

        return refreshed, self.metadata_backfill_attempts
