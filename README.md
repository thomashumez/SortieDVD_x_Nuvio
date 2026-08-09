# Guide-Rapide -> Nuvio/Stremio Static Catalog

This repository builds a fully static Nuvio/Stremio-compatible addon from public Guide-Rapide pages.

## Source

- https://www.guide-rapide.com/accueil.html
- https://www.guide-rapide.com/fluxrss.xml

RSS is only used for discovery. DVD/Blu-ray release dates and the source link are parsed from movie pages.

Guide-Rapide is not used as a movie-artwork or metadata provider. The builder uses OMDb for movie metadata and posters:

1. OMDb API (required by the production workflow)
2. Optional TMDB/IMDb behavior only when explicitly selected for local runs

OMDb is IMDb-id centric (`tt...`) and returns stable movie metadata and poster URLs in one API call. If OMDb cannot provide a poster, the production build fails validation instead of publishing a Guide-Rapide poster.

Enrichment includes:

- OMDb poster
- OMDb title, synopsis, rating, votes, runtime, genres, cast, and director
- OMDb writers / production companies
- OMDb awards / metascore / content rating / box office
- OMDb critic ratings map when available
- Guide-Rapide physical release dates and source attribution

## Output

The build script generates:

- `site/manifest.json`
- `site/catalog/movie/*.json`
- `site/meta/movie/*.json`

Main catalogs:

- DVD 12 mois - Production francaise (`dvd-12-mois-production-francaise`)
- DVD 12 mois - International (`dvd-12-mois-international`)
- Prochaines sorties (`prochaines-sorties`) when future physical release dates are present

## Local run

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python scripts/build_catalog.py
```

## Code layout

The CLI entrypoint stays intentionally small. The implementation is split by responsibility under `scripts/catalog/`:

- `config.py`: environment validation, paths, URLs, and runtime limits
- `models.py`: cached movie and provider metadata models
- `utils.py`: parsing primitives and atomic file I/O
- `http.py`: throttling, retries, request budgets, and failure telemetry
- `metadata.py`: OMDb/TMDB/IMDb enrichment and backfill
- `source.py`: Guide-Rapide discovery, cache, and incremental loading
- `parser.py`: Guide-Rapide movie-page extraction
- `output.py`: catalogs, metadata JSON, manifest validation, and publishing
- `builder.py`: orchestration only

Optional tuning for a bigger one-time backfill:

```bash
GR_DISCOVERY_MODE=full GR_FULL_ARCHIVE_PAGES=5000 GR_FULL_MOVIE_FETCH_PER_RUN=5000 python scripts/build_catalog.py
```

Strict OMDb metadata mode (used by GitHub Actions):

```bash
GR_METADATA_PROVIDER=omdb \
GR_REQUIRE_OMDB_METADATA=true \
GR_OMDB_API_KEY=your_key_here \
python scripts/build_catalog.py
```

Optional OMDb multi-key fallback (daily quota resilience):

```bash
GR_OMDB_API_KEYS=key1,key2,key3 python scripts/build_catalog.py
```

Notes:

- `GR_OMDB_API_KEY` is the primary key.
- `GR_OMDB_API_KEYS` is an optional comma-separated fallback pool.
- The builder automatically switches keys on OMDb `Invalid API key` or `Request limit reached` responses and skips unusable keys for the rest of that run.

Provider options:

- `GR_METADATA_PROVIDER=auto` (default): OMDb, then optional TMDB/IMDb behavior
- `GR_METADATA_PROVIDER=omdb`: use OMDb only
- `GR_METADATA_PROVIDER=tmdb`: force TMDB-only behavior
- `GR_METADATA_PROVIDER=imdb`: disable OMDb and keep IMDb-only behavior

Production metadata guard:

- `GR_REQUIRE_OMDB_METADATA=true`: requires `GR_METADATA_PROVIDER=omdb`, an OMDb key, an OMDb record, and an OMDb poster for every exported movie; the build fails before publishing if coverage is incomplete.
- Guide-Rapide image URLs are removed from cached records and are never emitted as movie posters, backgrounds, or logos.

Metadata backfill mode:

- `GR_METADATA_BACKFILL_MODE=off`: disable metadata backfill pass
- `GR_METADATA_BACKFILL_MODE=smart` (default): backfill only missing poster/trailer
- `GR_METADATA_BACKFILL_MODE=deep`: recheck all cached IMDb items (up to `GR_MAX_METADATA_API_LOOKUPS_PER_RUN`)

Robustness switches:

- `GR_ENABLE_IMDB_SUGGESTION_FALLBACK=false` disables unofficial IMDb suggestion endpoint fallback
- `GR_ENABLE_IMDB_HTML_FALLBACK=false` disables IMDb HTML parsing fallback

Recommended production values are both `false` to stay API-only.

Optional tuning for production-country backfill used by the 12-month French/International catalogs:

```bash
GR_MAX_COUNTRY_BACKFILL_PER_RUN=300 GR_COUNTRY_BACKFILL_WINDOW_DAYS=180 python scripts/build_catalog.py
```

Crawl strategy (default):

- First run: full bootstrap crawl (`GR_DISCOVERY_MODE=auto`)
- Next runs: incremental crawl using cache/state

Optional tuning for incremental daily runs:

```bash
GR_DISCOVERY_MODE=incremental GR_INCREMENTAL_ARCHIVE_PAGES=150 GR_INCREMENTAL_MOVIE_FETCH_PER_RUN=100 python scripts/build_catalog.py
```

Selective acceleration while preserving Guide-Rapide politeness:

```bash
GR_GUIDE_RAPIDE_DELAY_SECONDS=0.5 \
GR_GUIDE_RAPIDE_TIMEOUT=30 \
GR_METADATA_API_DELAY_SECONDS=0.1 \
GR_METADATA_API_TIMEOUT=20 \
python scripts/build_catalog.py
```

Notes:

- `GR_GUIDE_RAPIDE_*` applies only to Guide-Rapide hosts.
- `GR_METADATA_API_*` applies to OMDb/TMDB/IMDb metadata calls.
- This allows faster metadata enrichment without increasing load on Guide-Rapide.

Then open `site/manifest.json` or `site/index.html`.

## GitHub Actions and Pages

Workflow file location:

- `.github/workflows/build.yml`

Triggers:

- Daily schedule (randomized slot in the night window 00:00-07:00 UTC)
- `workflow_dispatch`
- Push on main for relevant files

Manual workflow options are intentionally minimal:

- `discovery_mode`: `auto` / `full` / `incremental`
- `full_archive_pages`: full-mode archive discovery cap (default `4000`)
- `full_movie_fetch`: full-mode movie processing cap (default `2500`)
- `metadata_backfill_mode`: `off` / `smart` / `deep`
- `wipe_cache`: force cache bypass and a fresh full bootstrap

Pipeline:

1. Install Python dependencies
2. Run scraper/catalog builder
3. Upload `site/` as GitHub Pages artifact
4. Deploy with official Pages actions

## First-time GitHub settings

1. In repository settings, ensure Actions are enabled.
2. In repository settings, set Pages source to GitHub Actions.
3. Ensure default branch is `main` (or adjust workflow `push.branches`).

### Configure OMDb key securely (public repo)

Do not commit your API key in files.
Use a GitHub Actions secret:

1. Repository Settings -> Secrets and variables -> Actions
2. New repository secret
3. Name: `OMDB_API_KEY`
4. Value: your private OMDb API key

The workflow already reads this secret via `secrets.OMDB_API_KEY`, so nightly runs continue to work without exposing the key in Git history.

Optional (recommended for quota fallback):

1. New repository secret
2. Name: `OMDB_API_KEYS`
3. Value: `key1,key2,key3`

### Configure TMDB key securely (recommended)

Use a second GitHub Actions secret:

1. Repository Settings -> Secrets and variables -> Actions
2. New repository secret
3. Name: `TMDB_API_KEY`
4. Value: your private TMDB API key

TMDB is not used by the production workflow, which runs in strict OMDb mode. Keep the TMDB secret only if you intentionally use a non-production local/provider configuration.

Final manifest URL format:

- `https://USERNAME.github.io/REPOSITORY/manifest.json`

Current repository manifest URL:

- `https://thomashumez.github.io/SortieDVD_x_Nuvio/manifest.json`

## Polite scraping and caching

The scraper is designed to be respectful:

- Request retries and timeouts
- Small delay between requests
- Archive page cache (`data/cache/pages`)
- Per-movie cache (`data/cache/movies`)
- Recheck window to avoid re-downloading unchanged historical pages every run

The workflow also restores `data/cache` using `actions/cache` to preserve crawl history across runs.

Production safeguards:

- Runtime settings are validated before crawling; invalid limits, timeouts, or modes fail fast.
- Metadata requests share a hard per-run budget, including title-to-IMDb lookup calls.
- Cache and generated files are written atomically, so interrupted writes do not leave partial JSON.
- The static site is built in a temporary directory, schema-checked, and published only after validation.
- A cold-start run refuses to publish an empty catalog when discovery has failed.
- Per-request failures are logged by host and error type without logging API keys.

The previous cache namespace input (example: `v2`) is not required anymore.
If you need to invalidate remote caches in the future, you can change the cache key prefix in `.github/workflows/build.yml`.

Default run limits are intentionally conservative for daily automation:

- `GR_INCREMENTAL_ARCHIVE_PAGES` default: `150`
- `GR_INCREMENTAL_MOVIE_FETCH_PER_RUN` default: `100`
- `GR_FULL_ARCHIVE_PAGES` default: `4000`
- `GR_FULL_MOVIE_FETCH_PER_RUN` default: `2500`

This keeps daily runs focused on recent surfaces while preserving a deeper full bootstrap/backfill mode when needed.
