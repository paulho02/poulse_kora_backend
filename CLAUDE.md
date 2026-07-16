# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

FastAPI backend (`backend/`) generated from the `fastapi-starter` template, paired with a
`poulse_kora_app` mobile app (separate repo, currently empty) which will be the real client
going forward.

The template ships with a React Admin frontend (`frontend/`) used only for the template's own
admin UI. It is **not used by this project** and has been disabled (not deleted):
- `docker-compose.yml` / `docker-compose.ci.yml`: `frontend` service is commented out.
- `.github/workflows/test.yaml`: the frontend Docker build and Cypress steps are commented out.
If frontend work is ever needed again, uncomment these and `cd frontend && yarn && yarn start` per
the README.

## Commands

All commands assume Docker Compose for local dev (hot-reload is configured via `docker-compose.override.yml`).

```bash
# Start backend + postgres
docker compose up -d

# Apply DB migrations
docker compose exec backend alembic upgrade head

# Create the test database (one-time)
docker compose exec postgres createdb apptest -U postgres

# Run backend tests
docker compose run backend pytest --cov --cov-report term-missing

# Run a single test
docker compose run backend pytest tests/api/test_items.py::TestGetItems::test_get_items -v

# Generate a migration after changing models in app/models/
docker compose exec backend alembic revision --autogenerate -m 'message'

# Verify models are in sync with migrations (what CI checks)
docker compose exec backend alembic check

# Rebuild after adding a dependency (pyproject.toml)
docker compose up -d --build

# IPython shell with DB session (app.db) preloaded
docker compose exec backend python shell.py
```

Backend OpenAPI docs: `http://localhost:8000/docs/`.

Dependencies are managed with Poetry (`backend/pyproject.toml` / `poetry.lock`). Linting uses
`ruff` (`ruff check`, `ruff format`) and `isort`; both run via `pre-commit` (`pre-commit install`
after cloning).

## Architecture

- **App factory** (`backend/app/factory.py`): `create_app()` builds the FastAPI app, wires the API
  router under `settings.API_PATH` (`/api/v1`), mounts `fastapi-users`' auth/register/users routers,
  sets up CORS, and mounts `static/` as a catch-all SPA fallback (404s outside `/api` or `/docs`
  serve `static/index.html` — this is a vestige of serving the built frontend from the same
  container; harmless but only relevant if the frontend is ever re-enabled). `main.py` just calls
  `create_app()`.
- **Auth** (`backend/app/deps/users.py`): uses `fastapi-users` with JWT bearer auth
  (`fastapi_users.current_user()`), exposed as the `CurrentUser` / `CurrentSuperuser` typed
  dependencies. `User` model (`app/models/user.py`) extends `SQLAlchemyBaseUserTableUUID`, so user
  IDs are UUIDs.
- **DB layer** (`backend/app/db.py`, `app/deps/db.py`): async SQLAlchemy 2.0 (`asyncpg`). Models
  subclass `Base` (`DeclarativeBase`). Route handlers get a session via the `CurrentAsyncSession`
  dependency (one session per request, closed after).
- **Config** (`backend/app/core/config.py`): `pydantic-settings` `Settings`, values sourced from
  `.env`. Notably, `DATABASE_URL` is transparently swapped for `TEST_DATABASE_URL` when `pytest` is
  in `sys.modules` — so tests always run against the `apptest` database regardless of env config.
- **API routes** (`backend/app/api/`): one module per resource (`items.py`, `users.py`,
  `utils.py`), aggregated in `api/__init__.py`'s `api_router`. Route function names become the
  OpenAPI `operationId` (enforced unique by `use_route_names_as_operation_ids` in `factory.py`) —
  this matters because a frontend API client can be generated from the OpenAPI schema
  (`yarn genapi`, only relevant if the React Admin frontend is revived).
- **List endpoints follow the React Admin data-provider convention**: `app/deps/request_params.py`
  parses react-admin-style `sort`/`range` query params into skip/limit/order, and responses set a
  `Content-Range` header (`{skip}-{end}/{total}`). This convention exists purely because of the
  template's frontend; new endpoints for the mobile app don't need to follow it unless there's a
  reason to.
- **Migrations**: Alembic, `backend/alembic/versions/`. CI runs `alembic upgrade head` then
  `alembic check` to catch model/migration drift — always generate a migration when you change a
  model.
- **Tests** (`backend/tests/`): `pytest-asyncio` (`asyncio_mode = auto`), session-scoped `httpx.AsyncClient`
  built from `create_app()` in `conftest.py`. Each test function gets an implicit rollback via the
  `auto_rollback` fixture, but fixtures like `create_user`/`create_item` are session-scoped
  factories, not per-test — data persists across tests in a run and rollback is best-effort cleanup,
  not full isolation. `tests/utils.py` has `get_jwt_header(user)` for authenticating requests without
  hitting the login endpoint.
