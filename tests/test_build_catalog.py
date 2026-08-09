import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from scripts import build_catalog
from scripts.catalog import output as catalog_output


class BuildCatalogTests(unittest.TestCase):
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
