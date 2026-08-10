from __future__ import annotations

import os
import shutil
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .config import OUTPUT_DIR, STATE_FILE
from .models import Movie
from .utils import (
    atomic_write_text,
    normalize_provider_image_url,
    read_json,
    subtract_months,
    write_json,
)

class OutputMixin:

    def release_info_text(self, movie: Movie) -> str:
        parts = []
        if movie.dvd_release_date:
            parts.append(f"DVD: {movie.dvd_release_date}")
        if movie.bluray_release_date:
            parts.append(f"Blu-ray: {movie.bluray_release_date}")
        if movie.year:
            parts.append(f"Production: {movie.year}")
        return " | ".join(parts)

    def to_meta_preview(self, movie: Movie) -> dict:
        poster = normalize_provider_image_url(movie.poster)
        preview = {
            "id": movie.id,
            "type": "movie",
            "name": movie.title,
            "poster": poster,
            "releaseInfo": self.release_info_text(movie),
        }
        if movie.imdb_id:
            preview["imdb_id"] = movie.imdb_id
        if movie.released:
            preview["released"] = movie.released
        if movie.genres:
            preview["genres"] = movie.genres
        if movie.rating:
            preview["imdbRating"] = movie.rating
        if movie.synopsis:
            preview["description"] = movie.synopsis[:500]
        return preview

    def to_meta(self, movie: Movie) -> dict:
        poster = normalize_provider_image_url(movie.poster)
        description_parts = []
        if movie.synopsis:
            description_parts.append(movie.synopsis)
        if movie.release_text:
            description_parts.append(movie.release_text)

        meta = {
            "id": movie.id,
            "type": "movie",
            "name": movie.title,
            "poster": poster,
            "background": poster,
            "description": "\n\n".join(description_parts).strip(),
            "genres": movie.genres,
            "director": movie.director,
            "cast": movie.actors,
            "releaseInfo": self.release_info_text(movie),
            "country": " / ".join(movie.production_countries) if movie.production_countries else "",
            "language": "fr",
            "logo": poster,
            "links": [{"name": "Guide-Rapide", "category": "source", "url": movie.source_url}],
        }

        if movie.released:
            meta["released"] = movie.released
        if movie.rating:
            meta["imdbRating"] = movie.rating
        if movie.runtime:
            meta["runtime"] = movie.runtime
        if movie.voters is not None:
            meta["votes"] = str(movie.voters)
        if movie.content_rating:
            meta["contentRating"] = movie.content_rating
        if movie.box_office:
            meta["boxOffice"] = movie.box_office
        if movie.critic_ratings:
            meta["criticRatings"] = movie.critic_ratings
        if movie.writers:
            meta["writer"] = movie.writers
        if movie.awards:
            meta["awards"] = movie.awards
        if movie.metascore:
            meta["metascore"] = movie.metascore
        if movie.production_companies:
            meta["productionCompanies"] = movie.production_companies
        if movie.imdb_id:
            meta["imdb_id"] = movie.imdb_id
            meta["links"].append(
                {
                    "name": "IMDb",
                    "category": "imdb",
                    "url": f"https://www.imdb.com/title/{movie.imdb_id}/",
                }
            )
        if movie.trailer_url:
            meta["trailers"] = [{"source": "youtube", "type": "Trailer", "url": movie.trailer_url}]

        return meta

    def write_catalog(self, catalog_id: str, movies: list[Movie]) -> None:
        payload = {"metas": [self.to_meta_preview(m) for m in movies]}
        write_json(self.output_root / "catalog" / "movie" / f"{catalog_id}.json", payload)

    def build_catalogs(self, movies: list[Movie]) -> tuple[list[dict], dict[str, list[Movie]]]:
        today = datetime.now(timezone.utc).date().isoformat()
        last_12_months_cutoff = subtract_months(datetime.now(timezone.utc).date(), 12).isoformat()

        physical_movies = [m for m in movies if m.physical_available]
        physical_past = [m for m in physical_movies if m.released and m.released <= today]
        physical_future = [m for m in physical_movies if m.released and m.released > today]

        physical_past.sort(key=lambda m: m.released, reverse=True)
        physical_future.sort(key=lambda m: m.released)

        catalog_defs = [
            {
                "type": "movie",
                "id": "dvd-12-mois-production-francaise",
                "name": "DVD 12 mois - Production francaise",
            },
            {
                "type": "movie",
                "id": "dvd-12-mois-international",
                "name": "DVD 12 mois - International",
            },
        ]

        dvd_recent = [
            m
            for m in physical_past
            if m.dvd_release_date and m.dvd_release_date >= last_12_months_cutoff
        ]
        dvd_recent.sort(key=lambda m: m.dvd_release_date, reverse=True)

        dvd_recent_fr = [m for m in dvd_recent if self.is_french_production(m)]
        dvd_recent_international = [m for m in dvd_recent if not self.is_french_production(m)]

        catalogs: dict[str, list[Movie]] = {
            "dvd-12-mois-production-francaise": dvd_recent_fr,
            "dvd-12-mois-international": dvd_recent_international,
        }

        if physical_future:
            catalog_defs.append(
                {"type": "movie", "id": "prochaines-sorties", "name": "Prochaines sorties"}
            )
            catalogs["prochaines-sorties"] = physical_future

        return catalog_defs, catalogs

    def write_manifest(self, catalogs: list[dict]) -> None:
        manifest = {
            "id": "org.guiderapide.nuvio",
            "version": datetime.now(timezone.utc).strftime("%Y.%m.%d"),
            "name": "Sortie DVD Tracker",
            "description": "Catalogues francais de sorties DVD et Blu-ray issus de Guide-Rapide.",
            "logo": "https://www.guide-rapide.com/IMG/divers/favicon.ico",
            "background": "https://www.guide-rapide.com/IMG/divers/favicon.ico",
            "resources": [
                "catalog",
                {"name": "meta", "types": ["movie"], "idPrefixes": ["tt", "gr-film-"]},
            ],
            "types": ["movie"],
            "catalogs": catalogs,
        }
        write_json(self.output_root / "manifest.json", manifest)

    def write_index(
        self,
        total_movies: int,
        discovered_count: int,
        catalogs: list[dict],
    ) -> None:
        catalog_name_by_id: dict[str, str] = {}
        ordered_catalog_ids: list[str] = []
        for catalog in catalogs:
            catalog_id = str(catalog.get("id", "")).strip()
            if not catalog_id:
                continue
            if catalog_id not in ordered_catalog_ids:
                ordered_catalog_ids.append(catalog_id)
            catalog_name = str(catalog.get("name", catalog_id)).strip() or catalog_id
            catalog_name_by_id[catalog_id] = catalog_name

        generated_catalog_ids = [
            path.stem
            for path in sorted((self.output_root / "catalog" / "movie").glob("*.json"))
            if path.stem
        ]
        for catalog_id in generated_catalog_ids:
            if catalog_id not in ordered_catalog_ids:
                ordered_catalog_ids.append(catalog_id)

        catalog_links = []
        for catalog_id in ordered_catalog_ids:
            catalog_name = catalog_name_by_id.get(catalog_id, catalog_id.replace("-", " ").title())
            catalog_links.append(
                f'<li><a href="catalog/movie/{catalog_id}.json">{catalog_name}</a></li>'
            )

        html = [
            "<!doctype html>",
            '<html lang="fr">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            "<title>Guide-Rapide Nuvio Catalog</title>",
            "<style>body{font-family:system-ui,sans-serif;max-width:900px;margin:32px auto;padding:0 16px;line-height:1.45}code{background:#f3f3f3;padding:2px 6px;border-radius:4px}</style>",
            "</head>",
            "<body>",
            "<h1>Guide-Rapide Nuvio Catalog</h1>",
            "<p>Manifest URL: <code>manifest.json</code></p>",
            f"<p>Films physiques indexes: <strong>{total_movies}</strong></p>",
            f"<p>Liens films decouverts pendant le crawl: <strong>{discovered_count}</strong></p>",
            "<ul>",
            '<li><a href="manifest.json">manifest.json</a></li>',
            *catalog_links,
            "</ul>",
            "</body>",
            "</html>",
        ]
        atomic_write_text(self.output_root / "index.html", "\n".join(html))

    def validate_output(self, catalogs: list[dict], movies: list[Movie]) -> None:
        """Fail before publish if the static artifact is incomplete or malformed."""
        manifest = read_json(self.output_root / "manifest.json", default=None)
        if not isinstance(manifest, dict) or manifest.get("id") != "org.guiderapide.nuvio":
            raise RuntimeError("Generated manifest is missing or has an unexpected id")

        manifest_catalogs = manifest.get("catalogs")
        if not isinstance(manifest_catalogs, list):
            raise RuntimeError("Generated manifest catalogs are missing or malformed")
        expected_catalog_ids = {str(item["id"]) for item in catalogs}
        manifest_catalog_ids = {
            str(item.get("id"))
            for item in manifest_catalogs
            if isinstance(item, dict) and item.get("id")
        }
        if manifest_catalog_ids != expected_catalog_ids:
            raise RuntimeError("Generated manifest catalogs do not match exported catalogs")

        for catalog_id in expected_catalog_ids:
            payload = read_json(
                self.output_root / "catalog" / "movie" / f"{catalog_id}.json",
                default=None,
            )
            if not isinstance(payload, dict) or not isinstance(payload.get("metas"), list):
                raise RuntimeError(f"Generated catalog is missing or malformed: {catalog_id}")

        for movie in movies:
            meta_path = self.output_root / "meta" / "movie" / f"{movie.id}.json"
            payload = read_json(meta_path, default=None)
            if not isinstance(payload, dict) or not isinstance(payload.get("meta"), dict):
                raise RuntimeError(f"Generated metadata is missing or malformed: {movie.id}")
            if payload["meta"].get("id") != movie.id:
                raise RuntimeError(f"Generated metadata id mismatch: {movie.id}")

        if self.config.require_omdb_metadata:
            missing: list[tuple[str, str]] = []
            for movie in movies:
                provider_ok = movie.metadata_source == "omdb"
                poster_ok = bool(normalize_provider_image_url(movie.poster))
                if provider_ok and poster_ok:
                    continue

                reasons = []
                if not provider_ok:
                    source = movie.metadata_source or "none"
                    reasons.append(f"source={source}")
                if not poster_ok:
                    reasons.append("poster=missing")
                missing.append((movie.id, "+".join(reasons)))

            if missing:
                sample = ", ".join(f"{movie_id}({reason})" for movie_id, reason in missing[:10])
                suffix = "..." if len(missing) > 10 else ""
                raise RuntimeError(
                    "OMDb metadata/poster coverage is incomplete: "
                    f"{len(missing)}/{len(movies)} missing ({sample}{suffix}); "
                    f"metadata_api_requests={self.metadata_api_requests}/"
                    f"{self.config.max_metadata_api_lookups_per_run}"
                )

    def publish_output(self, staging_dir: Path) -> None:
        """Atomically replace the previous generated site after validation."""
        OUTPUT_DIR.parent.mkdir(parents=True, exist_ok=True)
        backup_dir = OUTPUT_DIR.parent / f".{OUTPUT_DIR.name}.backup-{os.getpid()}-{time.time_ns()}"
        had_output = OUTPUT_DIR.exists()

        try:
            if had_output:
                OUTPUT_DIR.replace(backup_dir)
            staging_dir.replace(OUTPUT_DIR)
        except Exception:
            if not OUTPUT_DIR.exists() and backup_dir.exists():
                backup_dir.replace(OUTPUT_DIR)
            raise
        else:
            if backup_dir.is_dir():
                shutil.rmtree(backup_dir)
            elif backup_dir.exists():
                backup_dir.unlink()

    def persist_state(self) -> None:
        self.state["last_run"] = datetime.now(timezone.utc).isoformat()
        write_json(STATE_FILE, self.state)
        self.write_imdb_cache()
