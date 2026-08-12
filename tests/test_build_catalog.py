import json
import os
import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup

from scripts import build_catalog
from scripts.catalog import output as catalog_output
from scripts.catalog.models import ImdbMetadata, Movie
from scripts.catalog.utils import normalize_provider_image_url


class BuildCatalogTests(unittest.TestCase):
    def make_movie(self, poster: str = "") -> Movie:
        return Movie(
            id="tt1234567",
            source_url="https://www.guide-rapide.com/film-1.html",
            guide_rapide_id=1,
            title="Source title",
            year=2026,
            director=["Source director"],
            actors=["Source actor"],
            runtime="1h30",
            genres=["Drame"],
            synopsis="Source synopsis",
            rating="6.0",
            voters=10,
            poster=poster,
            trailer_url="",
            writers=[],
            production_companies=[],
            critic_ratings={},
            content_rating="",
            box_office="",
            awards="",
            metascore="",
            imdb_id="tt1234567",
            production_countries=["France"],
            dvd_release_date="2026-08-01",
            bluray_release_date="",
            release_type="dvd",
            release_text="DVD: 1 août 2026",
            released="2026-08-01",
            physical_available=True,
            checked_at="2026-08-09T00:00:00+00:00",
        )

    def test_invalid_runtime_configuration_fails_with_variable_name(self) -> None:
        with patch.dict(os.environ, {"GR_GUIDE_RAPIDE_TIMEOUT": "not-a-number"}):
            with self.assertRaisesRegex(ValueError, "GR_GUIDE_RAPIDE_TIMEOUT"):
                build_catalog.BuildConfig.from_env()

    def test_atomic_json_write_leaves_valid_file_and_no_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "nested" / "payload.json"

            build_catalog.write_json(path, {"ok": True, "items": [1, 2]})

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"ok": True, "items": [1, 2]})
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_metadata_budget_is_enforced_at_request_boundary(self) -> None:
        config = replace(
            build_catalog.BuildConfig.from_env(),
            max_metadata_api_lookups_per_run=1,
        )
        builder = build_catalog.GuideRapideBuilder(config=config)
        try:
            self.assertTrue(builder.reserve_request_budget("metadata_api"))
            self.assertFalse(builder.reserve_request_budget("metadata_api"))
            self.assertEqual(builder.metadata_api_requests, 1)
            self.assertTrue(builder.reserve_request_budget("guide_rapide"))
        finally:
            builder.close()

    def test_metadata_budget_zero_is_unlimited(self) -> None:
        config = replace(
            build_catalog.BuildConfig.from_env(),
            max_metadata_api_lookups_per_run=0,
        )
        builder = build_catalog.GuideRapideBuilder(config=config)
        try:
            for _ in range(20):
                self.assertTrue(builder.reserve_request_budget("metadata_api"))
            self.assertEqual(builder.metadata_api_requests, 20)
        finally:
            builder.close()

    def test_guide_rapide_host_matching_does_not_accept_lookalikes(self) -> None:
        builder = build_catalog.GuideRapideBuilder()
        try:
            self.assertTrue(builder.is_internal("https://www.guide-rapide.com/film-1.html"))
            self.assertTrue(builder.is_internal("https://sub.guide-rapide.com/film-1.html"))
            self.assertFalse(builder.is_internal("https://notguide-rapide.com/film-1.html"))
            self.assertEqual(
                builder.request_profile("https://notguide-rapide.com/film-1.html")[0],
                "default",
            )
        finally:
            builder.close()

    def test_naive_cached_timestamp_is_treated_as_utc(self) -> None:
        parsed = build_catalog.parse_timestamp("2026-08-09T12:00:00")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.tzinfo, build_catalog.timezone.utc)

    def test_french_release_date_parser_has_month_mapping(self) -> None:
        parsed = build_catalog.parse_french_date("27 mai 2026")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.date().isoformat(), "2026-05-27")

    def test_source_artwork_is_never_exported_as_movie_poster(self) -> None:
        builder = build_catalog.GuideRapideBuilder()
        try:
            source_html = '<meta property="og:image" content="https://www.guide-rapide.com/IMG/affiches/poster.jpg">'
            self.assertEqual(
                builder.extract_poster(BeautifulSoup(source_html, "lxml"), "https://www.guide-rapide.com/film-1.html"),
                "",
            )

            movie = self.make_movie(
                poster="https://www.guide-rapide.com/IMG/affiches/poster.jpg"
            )
            self.assertEqual(builder.to_meta_preview(movie)["poster"], "")
            full_meta = builder.to_meta(movie)
            self.assertEqual(full_meta["poster"], "")
            self.assertEqual(full_meta["background"], "")
            self.assertEqual(full_meta["logo"], "")
        finally:
            builder.close()

    def test_provider_image_normalization_treats_na_as_missing(self) -> None:
        self.assertEqual(normalize_provider_image_url("N/A"), "")
        self.assertEqual(normalize_provider_image_url("na"), "")
        self.assertEqual(normalize_provider_image_url("None"), "")

    def test_trailer_is_serialized_with_youtube_id(self) -> None:
        builder = build_catalog.GuideRapideBuilder()
        try:
            movie = self.make_movie()
            movie.trailer_url = "https://www.youtube.com/watch?v=3EssobOi3wE"

            meta = builder.to_meta(movie)
            self.assertEqual(meta.get("trailers"), [{"source": "3EssobOi3wE", "type": "Trailer"}])

            movie.trailer_url = "https://www.youtube.com/results?search_query=movie+trailer"
            meta = builder.to_meta(movie)
            self.assertNotIn("trailers", meta)
            trailer_links = [item for item in meta.get("links", []) if item.get("category") == "trailer"]
            self.assertEqual(len(trailer_links), 1)
        finally:
            builder.close()

    def test_omdb_metadata_sets_provider_and_poster(self) -> None:
        builder = build_catalog.GuideRapideBuilder()
        try:
            metadata = builder.omdb_payload_to_metadata(
                {
                    "Response": "True",
                    "Title": "API title",
                    "Year": "2025",
                    "Director": "API director",
                    "Actors": "API actor",
                    "Runtime": "105 min",
                    "Genre": "Drama",
                    "Plot": "API synopsis",
                    "imdbRating": "7.1",
                    "imdbVotes": "1,234",
                    "Poster": "https://m.media-amazon.com/images/poster.jpg",
                }
            )
            self.assertEqual(metadata.provider, "omdb")
            self.assertEqual(metadata.title, "API title")
            self.assertEqual(metadata.poster, "https://m.media-amazon.com/images/poster.jpg")

            movie = self.make_movie(
                poster="https://www.guide-rapide.com/IMG/affiches/poster.jpg"
            )
            with patch.object(builder, "fetch_imdb_metadata", return_value=metadata):
                self.assertTrue(builder.apply_imdb_metadata(movie))
            self.assertEqual(movie.metadata_source, "omdb")
            self.assertEqual(movie.title, "API title")
            self.assertEqual(movie.poster, "https://m.media-amazon.com/images/poster.jpg")
        finally:
            builder.close()

    def test_omdb_key_fallback_skips_unusable_key_for_rest_of_run(self) -> None:
        config = replace(
            build_catalog.BuildConfig.from_env(),
            metadata_provider="omdb",
            omdb_api_key="key-one",
            omdb_api_keys_raw="key-two,key-three,key-one",
        )
        builder = build_catalog.GuideRapideBuilder(config=config)
        try:
            responses = [
                {"Response": "False", "Error": "Invalid API key!"},
                {"Response": "True", "Title": "First success"},
                {"Response": "True", "Title": "Second success"},
            ]
            with patch.object(builder, "fetch_json", side_effect=responses) as fetch_json:
                first = builder.fetch_omdb_json_with_key_fallback({"i": "tt1234567"})
                second = builder.fetch_omdb_json_with_key_fallback({"i": "tt7654321"})

            self.assertEqual(first["Title"], "First success")
            self.assertEqual(second["Title"], "Second success")
            self.assertEqual(builder.omdb_api_keys, ["key-one", "key-two", "key-three"])
            self.assertEqual(builder.omdb_unusable_keys, {"key-one"})
            attempted_keys = [
                call.kwargs["params"]["apikey"] for call in fetch_json.call_args_list
            ]
            self.assertEqual(attempted_keys, ["key-one", "key-two", "key-two"])
        finally:
            builder.close()

    def test_omdb_key_fallback_blacklists_http_key_failures(self) -> None:
        config = replace(
            build_catalog.BuildConfig.from_env(),
            metadata_provider="omdb",
            omdb_api_key="key-one",
            omdb_api_keys_raw="key-two",
        )
        builder = build_catalog.GuideRapideBuilder(config=config)
        try:
            attempted_keys: list[str] = []

            def fake_fetch_json(url: str, *, params: dict[str, str]) -> dict | None:
                key = params["apikey"]
                attempted_keys.append(key)
                if key == "key-one":
                    builder.last_http_status = 401
                    return None
                builder.last_http_status = 200
                return {"Response": "True", "Title": "API success"}

            with patch.object(builder, "fetch_json", side_effect=fake_fetch_json):
                first = builder.fetch_omdb_json_with_key_fallback({"i": "tt1234567"})
                second = builder.fetch_omdb_json_with_key_fallback({"i": "tt7654321"})

            self.assertEqual(first["Title"], "API success")
            self.assertEqual(second["Title"], "API success")
            self.assertEqual(attempted_keys, ["key-one", "key-two", "key-two"])
            self.assertEqual(builder.omdb_unusable_keys, {"key-one"})
        finally:
            builder.close()

    def test_required_omdb_mode_requires_an_api_key(self) -> None:
        config = replace(
            build_catalog.BuildConfig.from_env(),
            metadata_provider="omdb",
            require_omdb_metadata=True,
            omdb_api_key="",
            omdb_api_keys_raw="",
        )
        with self.assertRaisesRegex(RuntimeError, "GR_REQUIRE_OMDB_METADATA"):
            build_catalog.GuideRapideBuilder(config=config)

    def test_strict_omdb_mode_uses_cached_omdb_without_network(self) -> None:
        config = replace(
            build_catalog.BuildConfig.from_env(),
            metadata_provider="omdb",
            require_omdb_metadata=True,
            omdb_api_key="dummy-key",
            omdb_api_keys_raw="",
        )
        builder = build_catalog.GuideRapideBuilder(config=config)
        try:
            cached_meta = ImdbMetadata(
                title="Cached title",
                poster="https://m.media-amazon.com/images/poster.jpg",
                provider="omdb",
            )
            builder.imdb_cache["tt1234567"] = asdict(cached_meta)

            with patch.object(builder, "fetch_omdb_metadata_by_imdb_id") as fetch_omdb:
                with patch.object(builder, "fetch_tmdb_metadata_by_imdb_id") as fetch_tmdb:
                    metadata = builder.fetch_imdb_metadata("tt1234567", allow_backfill=True)

            self.assertEqual(metadata.provider, "omdb")
            self.assertEqual(metadata.poster, "https://m.media-amazon.com/images/poster.jpg")
            fetch_omdb.assert_not_called()
            fetch_tmdb.assert_not_called()
        finally:
            builder.close()

    def test_strict_omdb_mode_repairs_missing_poster_without_tmdb(self) -> None:
        config = replace(
            build_catalog.BuildConfig.from_env(),
            metadata_provider="omdb",
            require_omdb_metadata=True,
            omdb_api_key="dummy-key",
            omdb_api_keys_raw="",
        )
        builder = build_catalog.GuideRapideBuilder(config=config)
        try:
            builder.imdb_cache["tt1234567"] = asdict(
                ImdbMetadata(title="Cached title", poster="", provider="omdb")
            )
            repaired = ImdbMetadata(
                title="Cached title",
                poster="https://m.media-amazon.com/images/repaired.jpg",
                provider="omdb",
            )

            with patch.object(
                builder,
                "fetch_omdb_metadata_by_imdb_id",
                return_value=repaired,
            ) as fetch_omdb:
                with patch.object(builder, "fetch_tmdb_metadata_by_imdb_id") as fetch_tmdb:
                    metadata = builder.fetch_imdb_metadata("tt1234567", allow_backfill=True)

            self.assertEqual(metadata.provider, "omdb")
            self.assertEqual(metadata.poster, "https://m.media-amazon.com/images/repaired.jpg")
            fetch_omdb.assert_called_once_with("tt1234567")
            fetch_tmdb.assert_not_called()
        finally:
            builder.close()

    def test_strict_omdb_mode_uses_tmdb_when_omdb_poster_is_na(self) -> None:
        config = replace(
            build_catalog.BuildConfig.from_env(),
            metadata_provider="omdb",
            require_omdb_metadata=True,
            omdb_api_key="dummy-key",
            tmdb_api_key="dummy-tmdb",
            omdb_api_keys_raw="",
        )
        builder = build_catalog.GuideRapideBuilder(config=config)
        try:
            omdb_meta = ImdbMetadata(
                title="Title from OMDb",
                poster="N/A",
                provider="omdb",
            )
            tmdb_meta = ImdbMetadata(
                title="Title from TMDb",
                poster="https://image.tmdb.org/t/p/w780/poster.jpg",
                provider="tmdb",
            )

            with patch.object(
                builder,
                "fetch_omdb_metadata_by_imdb_id",
                return_value=omdb_meta,
            ) as fetch_omdb:
                with patch.object(
                    builder,
                    "fetch_tmdb_metadata_by_imdb_id",
                    return_value=tmdb_meta,
                ) as fetch_tmdb:
                    metadata = builder.fetch_imdb_metadata("tt1234567", allow_backfill=True)

            self.assertEqual(metadata.provider, "omdb")
            self.assertEqual(metadata.poster, "https://image.tmdb.org/t/p/w780/poster.jpg")
            fetch_omdb.assert_called_once_with("tt1234567")
            fetch_tmdb.assert_called_once_with(
                "tt1234567",
                allow_when_omdb=True,
                include_details=False,
            )
        finally:
            builder.close()

    def test_tmdb_first_mode_prefers_tmdb_and_skips_omdb_when_poster_exists(self) -> None:
        config = replace(
            build_catalog.BuildConfig.from_env(),
            metadata_provider="tmdb",
            require_omdb_metadata=False,
            tmdb_api_key="dummy-tmdb",
            omdb_api_key="dummy-omdb",
            omdb_api_keys_raw="",
        )
        builder = build_catalog.GuideRapideBuilder(config=config)
        try:
            builder.imdb_cache.pop("tt1234567", None)
            builder.imdb_cache.pop(builder.unresolved_imdb_cache_key("tt1234567"), None)

            tmdb_meta = ImdbMetadata(
                title="Title TMDb",
                poster="https://image.tmdb.org/t/p/w780/from-tmdb.jpg",
                description="Plot from TMDb",
                provider="tmdb",
            )

            with patch.object(
                builder,
                "fetch_tmdb_metadata_by_imdb_id",
                return_value=tmdb_meta,
            ) as fetch_tmdb:
                with patch.object(
                    builder,
                    "fetch_omdb_metadata_by_imdb_id",
                ) as fetch_omdb:
                    metadata = builder.fetch_imdb_metadata("tt1234567", allow_backfill=True)

            self.assertEqual(metadata.provider, "tmdb")
            self.assertEqual(metadata.title, "Title TMDb")
            self.assertEqual(metadata.poster, "https://image.tmdb.org/t/p/w780/from-tmdb.jpg")
            self.assertEqual(metadata.description, "Plot from TMDb")
            fetch_tmdb.assert_called_once_with(
                "tt1234567",
                allow_when_omdb=True,
                include_details=False,
            )
            fetch_omdb.assert_not_called()
        finally:
            builder.close()

    def test_tmdb_first_mode_falls_back_to_omdb_when_tmdb_poster_missing(self) -> None:
        config = replace(
            build_catalog.BuildConfig.from_env(),
            metadata_provider="tmdb",
            require_omdb_metadata=False,
            tmdb_api_key="dummy-tmdb",
            omdb_api_key="dummy-omdb",
            omdb_api_keys_raw="",
        )
        builder = build_catalog.GuideRapideBuilder(config=config)
        try:
            builder.imdb_cache.pop("tt1234567", None)
            builder.imdb_cache.pop(builder.unresolved_imdb_cache_key("tt1234567"), None)

            tmdb_meta = ImdbMetadata(
                title="Title TMDb",
                poster="",
                provider="tmdb",
            )
            omdb_meta = ImdbMetadata(
                title="Title OMDb",
                poster="https://m.media-amazon.com/images/from-omdb.jpg",
                provider="omdb",
            )

            with patch.object(
                builder,
                "fetch_tmdb_metadata_by_imdb_id",
                return_value=tmdb_meta,
            ) as fetch_tmdb:
                with patch.object(
                    builder,
                    "fetch_omdb_metadata_by_imdb_id",
                    return_value=omdb_meta,
                ) as fetch_omdb:
                    metadata = builder.fetch_imdb_metadata("tt1234567", allow_backfill=True)

            self.assertEqual(metadata.provider, "tmdb")
            self.assertEqual(metadata.poster, "https://m.media-amazon.com/images/from-omdb.jpg")
            fetch_tmdb.assert_called_once_with(
                "tt1234567",
                allow_when_omdb=True,
                include_details=False,
            )
            fetch_omdb.assert_called_once_with("tt1234567")
        finally:
            builder.close()

    def test_unresolved_imdb_cooldown_defers_retry(self) -> None:
        config = replace(
            build_catalog.BuildConfig.from_env(),
            unresolved_imdb_retry_days=7,
        )
        builder = build_catalog.GuideRapideBuilder(config=config)
        try:
            now = datetime.now(timezone.utc)
            builder.imdb_cache[builder.unresolved_imdb_cache_key("tt1234567")] = {
                "failed_at": (now - timedelta(days=2)).isoformat(),
                "attempts": 2,
            }
            self.assertTrue(builder.should_defer_unresolved_imdb("tt1234567"))

            builder.imdb_cache[builder.unresolved_imdb_cache_key("tt1234567")] = {
                "failed_at": (now - timedelta(days=10)).isoformat(),
                "attempts": 3,
            }
            self.assertFalse(builder.should_defer_unresolved_imdb("tt1234567"))
        finally:
            builder.close()

    def test_publish_replaces_previous_site_as_a_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "site"
            target.mkdir()
            (target / "old.txt").write_text("old", encoding="utf-8")
            staging = root / "staging"
            staging.mkdir()
            (staging / "new.txt").write_text("new", encoding="utf-8")

            builder = build_catalog.GuideRapideBuilder()
            try:
                with patch.object(catalog_output, "OUTPUT_DIR", target):
                    builder.publish_output(staging)
            finally:
                builder.close()

            self.assertEqual((target / "new.txt").read_text(encoding="utf-8"), "new")
            self.assertFalse((target / "old.txt").exists())

    def test_tmdb_merge_overrides_physical_release_and_keeps_cinema_date(self) -> None:
        builder = build_catalog.GuideRapideBuilder()
        try:
            base = self.make_movie()
            base.physical_release_date = "2026-08-01"
            base.cinema_release_date = ""

            tmdb = self.make_movie()
            tmdb.tmdb_id = 123
            tmdb.physical_release_date = "2026-08-12"
            tmdb.cinema_release_date = "2026-05-04"
            tmdb.release_text = "Physical (TMDB FR): 2026-08-12"

            changed = builder.merge_tmdb_movie_data(base, tmdb)

            self.assertTrue(changed)
            self.assertEqual(base.physical_release_date, "2026-08-12")
            self.assertEqual(base.released, "2026-08-12")
            self.assertEqual(base.cinema_release_date, "2026-05-04")
            self.assertEqual(base.tmdb_id, 123)
            self.assertIn("TMDB", base.release_text)
        finally:
            builder.close()

    def test_tmdb_release_refresh_updates_existing_movie_dates(self) -> None:
        config = replace(
            build_catalog.BuildConfig.from_env(),
            tmdb_api_key="dummy-tmdb",
        )
        builder = build_catalog.GuideRapideBuilder(config=config)
        try:
            movie = self.make_movie()
            movie.tmdb_id = 999
            movie.release_text = "DVD: 1 août 2026"
            movie.cinema_release_date = ""

            release_payload = {
                "results": [
                    {
                        "iso_3166_1": "FR",
                        "release_dates": [
                            {"type": 4, "release_date": "2026-08-20T00:00:00.000Z"},
                            {"type": 5, "release_date": "2026-08-25T00:00:00.000Z"},
                            {"type": 6, "release_date": "2026-09-01T00:00:00.000Z"},
                            {"type": 3, "release_date": "2026-05-11T00:00:00.000Z"},
                        ],
                    },
                    {
                        "iso_3166_1": "US",
                        "release_dates": [
                            {"type": 4, "release_date": "2026-08-10T00:00:00.000Z"},
                            {"type": 5, "release_date": "2026-08-15T00:00:00.000Z"},
                            {"type": 6, "release_date": "2026-08-18T00:00:00.000Z"},
                        ],
                    },
                ]
            }

            with patch.object(builder, "fetch_tmdb_json", return_value=release_payload) as fetch_tmdb:
                checked, updated = builder.refresh_tmdb_release_dates_for_library([movie])

            self.assertEqual(checked, 1)
            self.assertEqual(updated, 1)
            self.assertEqual(movie.physical_release_date, "2026-08-10")
            self.assertEqual(movie.released, "2026-08-10")
            self.assertEqual(movie.release_type, "digital")
            self.assertEqual(movie.cinema_release_date, "2026-05-11")
            self.assertIn("TMDB releases", movie.release_text)
            self.assertTrue(fetch_tmdb.called)
        finally:
            builder.close()

    def test_tmdb_release_refresh_picks_lowest_date_across_types_4_5_6(self) -> None:
        config = replace(
            build_catalog.BuildConfig.from_env(),
            tmdb_api_key="dummy-tmdb",
        )
        builder = build_catalog.GuideRapideBuilder(config=config)
        try:
            movie = self.make_movie()
            movie.tmdb_id = 999

            release_payload = {
                "results": [
                    {
                        "iso_3166_1": "FR",
                        "release_dates": [
                            {"type": 4, "release_date": "2026-08-20T00:00:00.000Z"},
                            {"type": 5, "release_date": "2026-08-05T00:00:00.000Z"},
                            {"type": 6, "release_date": "2026-08-12T00:00:00.000Z"},
                        ],
                    },
                    {
                        "iso_3166_1": "US",
                        "release_dates": [
                            {"type": 4, "release_date": "2026-08-10T00:00:00.000Z"},
                        ],
                    },
                ]
            }

            with patch.object(builder, "fetch_tmdb_json", return_value=release_payload):
                checked, updated = builder.refresh_tmdb_release_dates_for_library([movie])

            self.assertEqual(checked, 1)
            self.assertEqual(updated, 1)
            self.assertEqual(movie.physical_release_date, "2026-08-05")
            self.assertEqual(movie.released, "2026-08-05")
            self.assertEqual(movie.release_type, "physical")
        finally:
            builder.close()

    def test_tmdb_release_refresh_incremental_checks_only_upcoming_or_missing_dates(self) -> None:
        config = replace(
            build_catalog.BuildConfig.from_env(),
            tmdb_api_key="dummy-tmdb",
        )
        builder = build_catalog.GuideRapideBuilder(config=config)
        try:
            today = datetime.now(timezone.utc).date()

            past_movie = self.make_movie()
            past_date = (today - timedelta(days=30)).isoformat()
            past_movie.physical_release_date = past_date
            past_movie.released = past_date

            upcoming_movie = self.make_movie()
            future_date = (today + timedelta(days=30)).isoformat()
            upcoming_movie.physical_release_date = future_date
            upcoming_movie.released = future_date

            missing_date_movie = self.make_movie()
            missing_date_movie.physical_release_date = ""
            missing_date_movie.released = ""

            release_payload = {
                "results": [
                    {
                        "iso_3166_1": "US",
                        "release_dates": [
                            {"type": 4, "release_date": "2026-08-10T00:00:00.000Z"},
                        ],
                    }
                ]
            }

            with patch.object(builder, "resolve_tmdb_id_for_movie", return_value=999) as resolve_tmdb_id:
                with patch.object(builder, "fetch_tmdb_json", return_value=release_payload):
                    checked, updated = builder.refresh_tmdb_release_dates_for_library(
                        [past_movie, upcoming_movie, missing_date_movie],
                        full_scan=False,
                    )

            self.assertEqual(resolve_tmdb_id.call_count, 2)
            self.assertEqual(checked, 2)
            self.assertEqual(updated, 2)
        finally:
            builder.close()

    def test_meta_includes_cinema_and_physical_release_fields(self) -> None:
        builder = build_catalog.GuideRapideBuilder()
        try:
            movie = self.make_movie()
            movie.physical_release_date = "2026-08-15"
            movie.cinema_release_date = "2026-03-10"

            meta = builder.to_meta(movie)
            self.assertEqual(meta.get("physicalRelease"), "2026-08-15")
            self.assertEqual(meta.get("cinemaRelease"), "2026-03-10")
            self.assertEqual(meta.get("releaseInfo"), "2026-08-15")
        finally:
            builder.close()


if __name__ == "__main__":
    unittest.main()
