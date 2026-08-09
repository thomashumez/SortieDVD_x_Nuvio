# Guide-Rapide -> Nuvio/Stremio Static Catalog

This repository builds a fully static Nuvio/Stremio-compatible addon from public Guide-Rapide pages.

## Source

- https://www.guide-rapide.com/accueil.html
- https://www.guide-rapide.com/fluxrss.xml

RSS is only used for discovery. DVD/Blu-ray release dates are parsed from movie pages.

When Guide-Rapide fields are missing, the builder can enrich from IMDb (using the movie IMDb id):

- Poster fallback
- Synopsis fallback
- Rating / votes fallback
- Runtime / genres fallback

## Output

The build script generates:

- `site/manifest.json`
- `site/catalog/movie/*.json`
- `site/meta/movie/*.json`

Main catalogs:

- DVD 12 mois - Production francaise (`dvd-3-mois-production-francaise`)
- DVD 12 mois - International (`dvd-3-mois-international`)
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

Optional tuning for production-country backfill used by the 12-month French/International catalogs:

```bash
GR_MAX_COUNTRY_BACKFILL_PER_RUN=300 GR_COUNTRY_BACKFILL_WINDOW_DAYS=180 python scripts/build_catalog.py
```

Crawl strategy (default):

- First run: full bootstrap crawl (`GR_DISCOVERY_MODE=auto`)
- Next runs: incremental crawl using cache/state

Optional tuning for incremental daily runs:

```bash
GR_DISCOVERY_MODE=incremental GR_INCREMENTAL_ARCHIVE_PAGES=850 GR_INCREMENTAL_MOVIE_FETCH_PER_RUN=320 python scripts/build_catalog.py
```

Then open `site/manifest.json` or `site/index.html`.

## GitHub Actions and Pages

Workflow file location:

- `.github/workflows/build.yml`

Triggers:

- Daily schedule (randomized slot in the night window 00:00-07:00 UTC)
- `workflow_dispatch`
- Push on main for relevant files

Pipeline:

1. Install Python dependencies
2. Run scraper/catalog builder
3. Upload `site/` as GitHub Pages artifact
4. Deploy with official Pages actions

## First-time GitHub settings

1. In repository settings, ensure Actions are enabled.
2. In repository settings, set Pages source to GitHub Actions.
3. Ensure default branch is `main` (or adjust workflow `push.branches`).

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

Default run limits are intentionally conservative for daily automation:

- `GR_MAX_ARCHIVE_PAGES` default: `850`
- `GR_MAX_MOVIE_FETCH_PER_RUN` default: `320`

This means the archive can be backfilled progressively across runs while still refreshing recent movies first.
