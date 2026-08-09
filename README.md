# Guide-Rapide -> Nuvio/Stremio Static Catalog

This repository builds a fully static Nuvio/Stremio-compatible addon from public Guide-Rapide pages.

## Source

- https://www.guide-rapide.com/accueil.html
- https://www.guide-rapide.com/fluxrss.xml

RSS is only used for discovery. DVD/Blu-ray release dates are parsed from movie pages.

When Guide-Rapide fields are missing, the builder enriches metadata with this priority:

1. OMDb API (if `GR_OMDB_API_KEY` is set)
2. TMDB API (if `GR_TMDB_API_KEY` is set)
3. IMDb fallback (optional)

This is the most Nuvio-friendly setup because OMDb is IMDb-id centric (`tt...`) and returns stable movie metadata in one API call. TMDB adds robust API fallback without HTML parsing.

Enrichment includes:

- Poster fallback
- Synopsis fallback
- Rating / votes fallback
- Runtime / genres fallback
- Writers / production companies
- Awards / metascore / content rating / box office
- Critic ratings map (IMDb / Rotten Tomatoes / Metacritic when available)

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
python scripts/build_catalog.py
```

Optional tuning for a bigger one-time backfill:

```bash
GR_DISCOVERY_MODE=full GR_FULL_ARCHIVE_PAGES=5000 GR_FULL_MOVIE_FETCH_PER_RUN=5000 python scripts/build_catalog.py
```

Optional API-first metadata mode (recommended for Nuvio):

```bash
GR_METADATA_PROVIDER=auto GR_OMDB_API_KEY=your_key_here GR_TMDB_API_KEY=your_key_here python scripts/build_catalog.py
```

Optional OMDb multi-key fallback (daily quota resilience):

```bash
GR_OMDB_API_KEYS=key1,key2,key3 python scripts/build_catalog.py
```

Notes:

- `GR_OMDB_API_KEY` is the primary key.
- `GR_OMDB_API_KEYS` is an optional comma-separated fallback pool.
- The builder automatically switches keys on OMDb `Invalid API key` or `Request limit reached` responses.

Provider options:

- `GR_METADATA_PROVIDER=auto` (default): try OMDb, then TMDB, then optional IMDb fallback
- `GR_METADATA_PROVIDER=omdb`: force OMDb-first behavior
- `GR_METADATA_PROVIDER=tmdb`: force TMDB-only behavior
- `GR_METADATA_PROVIDER=imdb`: disable OMDb and keep IMDb-only behavior

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

The workflow reads `secrets.TMDB_API_KEY` to keep metadata resilient when OMDb has gaps.

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

The previous cache namespace input (example: `v2`) is not required anymore.
If you need to invalidate remote caches in the future, you can change the cache key prefix in `.github/workflows/build.yml`.

Default run limits are intentionally conservative for daily automation:

- `GR_INCREMENTAL_ARCHIVE_PAGES` default: `150`
- `GR_INCREMENTAL_MOVIE_FETCH_PER_RUN` default: `100`
- `GR_FULL_ARCHIVE_PAGES` default: `4000`
- `GR_FULL_MOVIE_FETCH_PER_RUN` default: `2500`

This keeps daily runs focused on recent surfaces while preserving a deeper full bootstrap/backfill mode when needed.
