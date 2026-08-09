# AI Agent Context (Codex / Claude)

This file explains what this repository does and how to work on it safely.

## Project Purpose

Build a fully static Nuvio/Stremio-compatible addon catalog from public Guide-Rapide pages.

Main source pages:
- https://www.guide-rapide.com/accueil.html
- https://www.guide-rapide.com/fluxrss.xml

The script discovers movie pages, parses physical release dates (DVD/Blu-ray), enriches metadata via APIs, and publishes static JSON files under `site/`.

## Core Flow

1. Discover movie URLs from archive pages and RSS.
2. Fetch and parse each movie page.
3. Build a canonical movie id (`tt...` when available, otherwise `gr-film-...`).
4. Enrich movie metadata and posters from OMDb in the production workflow.
   Guide-Rapide remains the source for physical release facts and attribution only.
5. Build output catalogs and per-movie meta JSON.
6. Deploy `site/` via GitHub Pages workflow.

## Important Files

- `scripts/build_catalog.py`: stable CLI entrypoint; keep this file thin.
- `build_catalog.py`: repository-root convenience wrapper for the CLI.
- `scripts/catalog/builder.py`: top-level orchestration and HTTP session setup.
- `scripts/catalog/config.py`: validated environment configuration, paths, URLs, and limits.
- `scripts/catalog/models.py`: `Movie` and `ImdbMetadata` data models.
- `scripts/catalog/utils.py`: pure parsing helpers, logging, and atomic file I/O.
- `scripts/catalog/http.py`: request profiles, throttling, retries, metadata request budget, and failure telemetry.
- `scripts/catalog/metadata.py`: OMDb/TMDB/IMDb lookup, enrichment, and backfill logic.
- `scripts/catalog/source.py`: Guide-Rapide discovery, archive/RSS handling, cache, and incremental loading.
- `scripts/catalog/parser.py`: Guide-Rapide movie-page parsing and canonical ID construction.
- `scripts/catalog/output.py`: catalog/meta serialization, manifest validation, and staged site publishing.
- `tests/test_build_catalog.py`: regression tests for configuration, I/O, host validation, request budgets, and publishing.
- `.github/workflows/build.yml`: scheduled/manual/push automation and Pages deploy.
- `requirements.txt`: Python dependencies.
- `site/`: generated static addon payload.
- `data/cache/`: local cache and state for incremental behavior.

When changing behavior, edit the owning module under `scripts/catalog/` rather than putting new logic back into the CLI entrypoint.

## Output Contract

Generated files:
- `site/manifest.json`
- `site/catalog/movie/*.json`
- `site/meta/movie/*.json`

Important behavior:
- Catalog files are lightweight previews.
- Full metadata lives in `site/meta/movie/{id}.json`.
- The site is generated in a temporary directory, validated, and published as a directory swap.
- Cache and generated files use atomic writes; do not replace this with direct partial writes.
- Guide-Rapide image URLs are never emitted as movie posters, backgrounds, or logos.

## Run Modes

Discovery mode (`GR_DISCOVERY_MODE`):
- `auto`: first run full, next runs incremental
- `full`: force deep crawl
- `incremental`: fast update crawl

Metadata backfill mode (`GR_METADATA_BACKFILL_MODE`):
- `off`: no metadata backfill pass
- `smart`: only missing high-value fields (poster/trailer)
- `deep`: recheck cached IMDb items up to lookup cap

## Key Environment Variables

Provider / APIs:
- `GR_METADATA_PROVIDER=auto|omdb|tmdb|imdb`
- `GR_REQUIRE_OMDB_METADATA=true|false` (production workflow uses `true`)
- `GR_OMDB_API_KEY`
- `GR_OMDB_API_KEYS` (comma-separated fallback pool)
- `GR_TMDB_API_KEY`
- `GR_ENABLE_IMDB_SUGGESTION_FALLBACK=true|false`
- `GR_ENABLE_IMDB_HTML_FALLBACK=true|false`

Caps and limits:
- `GR_FULL_ARCHIVE_PAGES`
- `GR_FULL_MOVIE_FETCH_PER_RUN`
- `GR_INCREMENTAL_ARCHIVE_PAGES`
- `GR_INCREMENTAL_MOVIE_FETCH_PER_RUN`
- `GR_MAX_METADATA_API_LOOKUPS_PER_RUN`
- `GR_MAX_IMDB_POSTER_REFRESH_PER_RUN`
- `GR_COUNTRY_BACKFILL_WINDOW_DAYS`
- `GR_MAX_COUNTRY_BACKFILL_PER_RUN`

## Production Defaults (Current Workflow Intent)

In GitHub Actions, production requires OMDb metadata and posters:
- `GR_METADATA_PROVIDER=omdb`
- `GR_REQUIRE_OMDB_METADATA=true`
- `GR_ENABLE_IMDB_SUGGESTION_FALLBACK=false`
- `GR_ENABLE_IMDB_HTML_FALLBACK=false`

## Performance Notes

Current crawler behavior is intentionally conservative:
- request timeout and delay are validated runtime settings
- retries/backoff are enabled
- network calls are mostly sequential
- metadata API calls share the hard `GR_MAX_METADATA_API_LOOKUPS_PER_RUN` budget, including title-to-IMDb lookup calls

This favors stability and source politeness over raw speed.

## Safety and Repository Rules

- Never commit API keys or secrets.
- Use GitHub Actions secrets for OMDb/TMDB keys.
- Keep outputs static-only (no runtime server required).
- Preserve endpoint compatibility with Nuvio/Stremio consumers.
- Avoid changing output JSON shape unless explicitly requested.
- Do not publish an empty cold-start catalog when discovery and cache loading both return no movies.
- Keep provider secrets out of logs; request-failure telemetry may log host and error type only.
- OMDb keys that return an invalid-key or exhausted-quota response, including HTTP 401/403/429, are skipped for the remainder of the current run; the pool resets on the next run.
- Keep provider-specific logic in `metadata.py` until a provider-specific module split is explicitly introduced.

## Typical Local Commands

Install and run:

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python scripts/build_catalog.py
```

Syntax and dependency checks:

```bash
python -m py_compile scripts/build_catalog.py scripts/catalog/*.py tests/test_build_catalog.py
python -m pip check
```

API-enriched run:

```bash
GR_METADATA_PROVIDER=auto \
GR_OMDB_API_KEY=... \
GR_TMDB_API_KEY=... \
python scripts/build_catalog.py
```

Deep backfill example:

```bash
GR_METADATA_BACKFILL_MODE=deep \
GR_MAX_METADATA_API_LOOKUPS_PER_RUN=300 \
python scripts/build_catalog.py
```

## If You Are an AI Agent Editing This Repo

- Prefer minimal, focused edits.
- Keep cache/state logic intact unless asked.
- Preserve the CLI commands and the package boundaries under `scripts/catalog/`.
- Validate with the unit suite and a small cache-backed incremental run first.
- Treat `site/` and `data/cache/` as generated/runtime state; do not hand-edit or commit them.
- Report impact on:
  - discovery count
  - fetched count
  - metadata backfill lookup count
  - trailer coverage when relevant
