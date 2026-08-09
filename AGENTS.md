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
4. Enrich metadata with provider priority:
   - OMDb API
   - TMDB API
   - optional IMDb fallback (disabled in production workflow)
5. Build output catalogs and per-movie meta JSON.
6. Deploy `site/` via GitHub Pages workflow.

## Important Files

- `scripts/build_catalog.py`: main crawler, enricher, and static exporter.
- `build_catalog.py`: convenience entrypoint/wrapper.
- `.github/workflows/build.yml`: scheduled/manual/push automation and Pages deploy.
- `requirements.txt`: Python dependencies.
- `site/`: generated static addon payload.
- `data/cache/`: local cache and state for incremental behavior.

## Output Contract

Generated files:
- `site/manifest.json`
- `site/catalog/movie/*.json`
- `site/meta/movie/*.json`

Important behavior:
- Catalog files are lightweight previews.
- Full metadata lives in `site/meta/movie/{id}.json`.

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

In GitHub Actions, production is API-first and avoids fragile IMDb fallbacks:
- `GR_METADATA_PROVIDER=auto`
- `GR_ENABLE_IMDB_SUGGESTION_FALLBACK=false`
- `GR_ENABLE_IMDB_HTML_FALLBACK=false`

## Performance Notes

Current crawler behavior is intentionally conservative:
- request timeout is fixed in code
- request delay/throttle is fixed in code
- retries/backoff are enabled
- network calls are mostly sequential

This favors stability and source politeness over raw speed.

## Safety and Repository Rules

- Never commit API keys or secrets.
- Use GitHub Actions secrets for OMDb/TMDB keys.
- Keep outputs static-only (no runtime server required).
- Preserve endpoint compatibility with Nuvio/Stremio consumers.
- Avoid changing output JSON shape unless explicitly requested.

## Typical Local Commands

Install and run:

```bash
python -m pip install -r requirements.txt
python scripts/build_catalog.py
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
- Validate with a small incremental run first.
- Report impact on:
  - discovery count
  - fetched count
  - metadata backfill lookup count
  - trailer coverage when relevant
