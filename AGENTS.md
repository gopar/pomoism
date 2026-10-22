# AGENTS.md

Multi-machine pomodoro sync service. Requires Python 3.11+ (`tomllib`).
Developed/run on macOS.

Tests live in `tests/` (pytest + `unittest.mock`). Run them with:

    pytest

Add tests under `tests/` to verify changes; for anything that can't be covered
by a test, run the processes directly (see below).
Side effects (`shortcuts`, `say`, `afplay`, Emacs) are macOS-specific.

CI (`.github/workflows/ci.yml`) runs the pytest suite on Python 3.11–3.14,
a Django system check, `collectstatic`, ruff lint/format, ty, and a
`compileall` syntax gate (Ubuntu).

## Architecture (3 processes in the `pomo` package)

- `pomo/web/` — Django HTTP/JSON server plus the public dashboard. It exposes
  the legacy API paths plus `/api/` aliases. The persistence layer is the
  stdlib `sqlite3` store in `pomo/server.py` (last-write-wins), deliberately
  kept raw so its `BEGIN IMMEDIATE` race guarantees survive. Django owns
  migrations, auth tables, static files, and the read-only authenticated admin.
  One instance on a "home-base" machine. API endpoints: `GET /health`,
  `GET /version`, `GET /current`, `GET /sessions` (optional `?project=`,
  `?from=`, `?to=`, `?state=`), `GET /projects`, `GET /stats`, `POST /sessions`,
  `POST /sessions/end`, `PATCH /sessions/<id>`, `POST /sessions/<id>/archive`.
  Public UI: `/`, `/sessions/`, `/sessions/new/`, `/sessions/<id>/edit/`,
  `/sessions/<id>/delete/`. Admin: `/admin/` (login required, session data is
  read-only).
- `pomo/agent.py` — per-machine daemon. Polls `/current`, owns the countdown→overtime
  timer, fires side effects, flushes the offline outbox.
- `pomo/cli.py` — the CLI (`pomo start <min>`, `pomo break <min>`, `pomo clear`). Writes the
  local cache immediately, then pushes (or queues to outbox if offline).

`pomo/common.py` is imported by all three. Install with `pip install -e .` or
`uv tool install` to place `pomo`, `pomo-agent`, and `pomo-server` on `PATH`.
`pomo-server` starts Django + waitress and auto-migrates; it also dispatches
Django management commands (`pomo-server migrate`, `createsuperuser`, ...).

## Critical invariants — easy to break

- **LWW by `updated_at`**: every session mutation must set `updated_at = time.time()`
  or the server will reject it as stale (`applied: false`). See `apply_session` in
  `pomo/server.py` and `tick_timer` in `pomo/agent.py`. On the server this is race-safe:
  a staleness pre-read, the history insert, and a WHERE-guarded pointer UPDATE run in
  one `BEGIN IMMEDIATE` transaction (WAL + `busy_timeout`), so concurrent writers
  can't lose the newest write. `end_current` uses `apply_session(..., force=True)`,
  which stamps `updated_at` server-side and clamps it above the stored row, so an
  explicit stop always wins even against a clock-skewed client.
- **All session writes go through `pomo/server.py`**: Django views may read
  through the ORM, but session mutations must call the store functions
  (`apply_session`, `end_current`, `edit_session`, `archive_session`,
  `create_session`) so LWW, history, and the `current` pointer stay correct.
  Do not write `Session` rows from Django forms/admin. `create_session` is the
  historical-entry path and deliberately does not move `current`.
- **Public dashboard, protected admin**: public UI views must not use
  `login_required` or `LoginRequiredMiddleware`. Only `/admin/` is
  authenticated, and its session models are read-only.
- **`ended`/`archived` are terminal**: `apply_session` rejects non-terminal writes
  for a row already `ended`/`archived` — a just-woken machine's late overtime push
  must not resurrect a finished session.
- **`current` points at the latest-started session**: the pointer only moves to a
  *different* session when its `start_epoch` is later than the pointer session's,
  so a stale overtime push for an old session can't steal `current` from a newer
  one. Agents adopt a different-id remote session when `remote.start_epoch >
  local.start_epoch` (clock-skew safe) or when its `updated_at` is newer, and clear
  the cache when an idle marker carries their exact `session_id`.
- **`ended` is a real state, not deletion**: stops propagate as an `ended` record
  (a file deletion can't sync). Keep it in `ALL_STATES`; `is_idle()` treats
  `idle`/`ended`/`None` as idle.
- **Cache writes are atomic** (temp file + `replace`) so concurrent readers
  never see partial data. Preserve this pattern.
- States: `pomodoro`/`break` → `overtime`/`break-overtime` (via `OVERTIME_OF`) → `ended`.

## Side effects & hooks

- **All side effects are hooks** — the daemon ships no built-in effects and is
  OS-agnostic. Both CLI and agent fire events via `hooks.dispatch()` in
  `pomo/hooks.py` (the single entry point, so `pomo/cli.py` and `pomo/agent.py` don't import
  each other). Hooks are best-effort and must never crash the loop/CLI.
- User hooks: executables in `~/.config/pomo/hooks/<event>.d/*` (`pomo/hooks.py`), run in
  lexical order, killed after `hooks.timeout`. Events: `pomodoro_start`,
  `break_start`, `pomodoro_overtime`, `break_overtime`, `session_stop`. Ready-made examples
  (macOS + Linux, Windows stub) in `hooks/examples/<event>.d/`.
- `run_for_remote_sessions` (top-level config) gates whether adopting a
  remote-started session fires hooks (`on_remote_adopt` in `pomo/agent.py`).

## Config & paths

- Config: `~/.config/pomo/agent.toml`, merged over `_DEFAULT_CONFIG` in
  `pomo/common.py` (only `[hooks]` is deep-merged; other keys replace; the
  agent re-reads config every loop). Fresh files are written by
  `pomo config --init` from the `CONFIG_SAMPLE` string constant in
  `pomo/common.py` (the single source of truth for config docs; there is no
  separate sample file).
- Env overrides: `POMO_SERVER_URL` (agent/CLI), `POMO_TOKEN` (bearer auth, both
  ends), `POMO_PORT`/`POMO_HOST`/`POMO_DB_PATH` (server),
  `POMO_SECRET_KEY`/`POMO_SECRET_KEY_FILE`, `POMO_ALLOWED_HOSTS`,
  `POMO_DEBUG`, `POMO_TIME_ZONE`, `POMO_CSRF_TRUSTED_ORIGINS`,
  `POMO_STATIC_ROOT`/`POMO_AUTO_COLLECTSTATIC`, and `POMO_AUTO_MIGRATE`
  (Django server).
- Paths in `pomo/common.py`: cache `~/.cache/pomo/`, DB `~/.local/share/pomo/pomo.db`,
  generated Django secret `~/.config/pomo/django_secret_key`, collected static
  `~/.local/share/pomo/static`.
- Migrations: `pomo/web/migrations/0001_initial.py` must keep matching the
  legacy `sessions`/`current` tables so `migrate --fake-initial` can adopt an
  existing DB. Index/history migration operations must be idempotent because
  the store's `init_db()` also creates indexes.

## Testing Philosophy
- When adding tests, do your best to minimize mocking parts of the system unless its required.
- Prefer tests that test the behavior and not the internals.
- When fixing a bug, write a test to verify existing bug and then re-run it to verify it has been fix.
- Tests isolate all state onto a temp dir via `tests/conftest.py` (patches path
  globals in `common`/`server`); Django tests use `tests/settings.py` so they
  never touch real `~`.
- API tests use the Django test client against the raw store; keep
  `tests/test_server.py` as the coverage for LWW/concurrency invariants.
- UI/admin tests: public dashboard views must remain reachable anonymously;
  `/admin/` must redirect anonymous users to login. Admin session models are
  read-only. Stats aggregation is tested as pure functions in
  `tests/test_web_stats.py`.
- When adding tests use Gherkin style comments (Given, When, Then, etc)

## Pre-commit hooks

Install git hooks to run lint + type checks before every commit:

```sh
uv sync --dev
pre-commit install
```

Hooks run `ruff` (lint + format), `ty` (type check), and file-sanity checks
(`check-yaml`, `check-toml`, trailing whitespace, EOF newlines). CI also runs
these — pre-commit catches them before they reach CI.

When bumping the `ruff` version in `[dependency-groups]`, also update the
`ruff-pre-commit` mirror tag in `.pre-commit-config.yaml` to match.

## Linting

Ruff handles both linting and formatting. Run before committing:

```sh
ruff check .          # lint
ruff format .         # format
ruff check . --fix    # auto-fix (safe fixes only)
```

Type checking uses `ty` (fast, stdlib-friendly). Run on source only:

```sh
ty check src/pomo
```

CI enforces all three. Dev install includes ruff and ty: `uv sync --dev`

## Keeping README in sync

`README.md` is the user-facing documentation. It must stay in sync with the
code. When you add, change, or remove any of these, update `README.md`:

- **CLI flags/subcommands** — caught by snapshot tests in
  `tests/test_snapshots.py` (CI fails on mismatch)
- **Hook environment variables** — caught by `test_build_env_has_expected_pomo_keys`
  in `tests/test_hooks.py` (CI fails on mismatch)
- **Server API endpoints** — no automated check; update manually
- **Public UI routes and admin access** — update manually; public views must
  stay anonymous and `/admin/` must stay login-protected

## Versioning

When bumping the version:

- Update the `VERSION` file
- Commit as `vX.Y.Z`
- (optional) `git tag vX.Y.Z`
