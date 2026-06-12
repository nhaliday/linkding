# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project shape

linkding is a self-hosted bookmark manager built as a single Django project (`linkding`) with a single Django app named `bookmarks/` that holds essentially all application code. There is no `siteroot`/project-level Python package — `bookmarks/` plays both roles, so root settings/urls/wsgi live inside `bookmarks/`.

Python 3.13, Django 6, dependencies managed by **uv**. Frontend is a hand-rolled Rollup build (no framework runtime) using Lit for custom elements and Hotwired Turbo for navigation. Background work runs through **Huey** with a SQLite-backed queue (no Redis required).

## Common commands

Build/run/test (from the Makefile):

```shell
make init          # uv sync + create data/ dirs + run migrations + npm install
make serve         # Django dev server on :8000
make frontend      # rollup -c -w  (JS/CSS dev watcher)
make tasks         # uv run manage.py run_huey  (background worker)
make test          # uv run pytest -n auto
make lint          # ruff check bookmarks
make format        # ruff format + djlint templates + prettier JS/CSS
make e2e           # builds static assets, then runs Playwright tests
```

**`make init && make serve` alone produces a half-broken UI.** `make init` does not build the frontend — `bookmarks/static/bundle.js` and `theme-{light,dark}.css` are missing until you either run `make frontend` (Rollup watcher; JS only) or `npm run build` (one-shot: JS + both theme CSS files). Without the bundle, Lit-based custom elements like `<ld-modal>` render but their event handlers never bind, so things like the bookmark details modal's close button silently do nothing. Run `npm run build` once after `make init`, or run `make frontend` in a second terminal during development.

Run a single test or subset:

```shell
uv run pytest bookmarks/tests/test_bookmarks_api.py
uv run pytest bookmarks/tests/test_bookmarks_api.py::BookmarksApiTestCase::test_list_bookmarks
uv run pytest -k "search and tag"
```

Create a dev superuser:

```shell
uv run manage.py createsuperuser --username=joe --email=joe@example.com
```

`pytest.ini` pins `DJANGO_SETTINGS_MODULE=bookmarks.settings.dev`, so tests don't need a settings flag. E2E tests live in `bookmarks/tests_e2e/` and use `python_files=e2e_test_*.py` (so a plain `pytest` run skips them).

## Architecture you'll need to read multiple files to see

**Settings are split, runtime config is `LD_*` env vars.** `bookmarks/settings/base.py` is the shared base; `dev.py`, `prod.py`, and `custom.py` layer on top. User-facing options are all `LD_FOO` environment variables parsed in `base.py` — when adding a new option, define it there and document it in `docs/src/content/docs/options.md`.

**Services layer is where the business logic lives, not views.** `bookmarks/views/` is thin (form handling, context assembly, redirects). The real work — importing, exporting, archiving, auto-tagging, fetching website metadata, generating previews — lives in `bookmarks/services/*.py`. Reach for `bookmarks/services/` before adding logic to a view.

**Background tasks go through `bookmarks/services/tasks.py`.** It wraps `huey.task` with a custom retry decorator (exponential backoff: 60s → 240s → 960s → 3840s → 15360s). Long-running work (singlefile snapshots, favicon/preview loading, Wayback archival) must be a Huey task — don't block request handlers. The worker is started with `make tasks` in dev; in production `supervisord-all.conf` runs uwsgi + huey together.

**Two URL trees: HTML views and REST API.** `bookmarks/urls.py` mounts both — server-rendered Django views (`bookmarks/views/*`) for the UI and DRF routes (`bookmarks/api/routes.py` + `serializers.py`) for the JSON API. The HTML side relies heavily on Turbo for partial navigation; the API side is documented in `docs/src/content/docs/api.md`.

**Frontend is built, not bundled at runtime.** Rollup config in `rollup.config.mjs` compiles `bookmarks/frontend/index.js` and registers Lit-based custom elements from `bookmarks/frontend/components/` (tags like `<ld-tag-autocomplete>`, `<ld-modal>`, etc. — the `ld-*` prefix is configured in `pyproject.toml`'s djlint `custom_html`). Theme CSS is compiled separately via postcss. Output lands in `bookmarks/static/` and is served by Django; production builds run `collectstatic` into top-level `static/`.

**Models live in one file but tag/asset lifecycles use signals.** `bookmarks/models.py` defines `Bookmark`, `Tag`, `BookmarkAsset`, `BookmarkBundle`, `UserProfile`, etc. Side effects on save/delete (favicon refresh, asset cleanup, search-index updates) are wired via Django signals in `bookmarks/signals.py` — don't expect a service call to be the only path that fires them.

**Migrations are at `bookmarks/migrations/0054_*`** (and counting). Migration numbers move fast; when rebasing on upstream, expect to run `uv run manage.py makemigrations --merge` if you've added your own.

**Tests use Django's `TestCase` + `pytest-django`, with shared helpers in `bookmarks/tests/helpers.py`.** That helper module has factories (`setup_bookmark`, `setup_tag`, `setup_asset`) that virtually every test uses — read it before writing new tests. Performance-sensitive paths have dedicated `*_performance.py` files that assert query counts; if you change a hot path, expect to update one.

## Conventions worth knowing

- **Tag name comparisons are case-insensitive** but stored as-entered. `TagCache` in `bookmarks/services/importer.py` is the canonical pattern for bulk tag lookup.
- **URLs are normalized via `bookmarks/utils.py:normalize_url`** before storage and comparison. Use it whenever you accept URLs from users or APIs, or duplicates will leak in.
- **Templates use djlint with custom blocks** (`formhelp`) and the `ld-*` custom-element namespace, both declared in `pyproject.toml`. `make format` enforces this.
- **Don't add new dependencies casually** — the project's selling point is being light. Check `pyproject.toml` and `package.json` first; reuse what's there.

## Documentation source

User-facing docs live in `docs/src/content/docs/*.md` (Astro Starlight site published to linkding.link). When you add a feature with config or behavior changes, update the relevant page (`installation.md`, `options.md`, `api.md`, `how-to.md`, etc.) in the same change.
