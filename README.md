# Guide-Rapide -> Nuvio/Stremio Static Catalog

This repository builds a fully static Nuvio/Stremio-compatible addon from public Guide-Rapide pages.

## Source

- https://www.guide-rapide.com/accueil.html
- https://www.guide-rapide.com/fluxrss.xml

RSS is only used for discovery. DVD/Blu-ray release dates are parsed from movie pages.

## Output

The build script generates:

- `site/manifest.json`
- `site/catalog/movie/*.json`
- `site/meta/movie/*.json`

Main catalogs:

- DVD France - Nouveautes (`dvd-france-nouveautes`)
- DVD + Blu-ray France (`dvd-bluray-france`)
- Blu-ray France (`bluray-france`)
- Toutes les sorties physiques (`toutes-sorties-physiques`)
- Prochaines sorties (`prochaines-sorties`) when future physical release dates are present
- Optional genre catalogs when enough items are available

## Local run

```bash
python -m pip install -r requirements.txt
python scripts/build_catalog.py
```

Optional tuning for a bigger one-time backfill:

```bash
GR_MAX_ARCHIVE_PAGES=2000 GR_MAX_MOVIE_FETCH_PER_RUN=2500 python scripts/build_catalog.py
```

Then open `site/manifest.json` or `site/index.html`.

## GitHub Actions and Pages

Workflow file location:

- `.github/workflows/build.yml`

Triggers:

- Daily schedule
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
