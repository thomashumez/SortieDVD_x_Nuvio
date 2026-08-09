import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup

from scripts import build_catalog
from scripts.catalog import output as catalog_output
from scripts.catalog.models import ImdbMetadata, Movie


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


if __name__ == "__main__":
    unittest.main()
