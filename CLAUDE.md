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

# Seed dev data: bot users + a few posts per channel, so a real dev/mobile-app
# account has something to see and review (forward/drop) after subscribing to
# a channel. Idempotent, safe to re-run.
docker compose exec backend python seed_dev_data.py
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
- **Delivery exclusions** (`backend/app/feed/`): two independent guards behind two flags.
  `FEED_EXCLUDE_OWN_POSTS` carries `author_id` on the stream entry so the worker skips the
  post's author — no stored state, no extra round trip. `FEED_EXCLUDE_SEEN` keeps a per-post
  `seen:{post_id}` set written **inside the `place` Lua script**, so a recipient is recorded
  atomically with delivery, before they could review or forward it. `select_recipients`
  filters on both (efficiency); `place_post` re-checks and returns `PLACE_REFUSED`
  (correctness) — it's the choke point every delivery path goes through, so never count a
  refusal as a delivery. Postgres' unique `(user, post)` review constraint remains the
  backstop, so a lost/expired set degrades to a 409 rather than breaking. **Enabling
  `FEED_EXCLUDE_SEEN` on an existing DB requires `python rebuild_redis.py`** to seed the
  sets from `post_reviews`. Consequence to know: exclusions make channel *saturation*
  reachable, so `process_operation` now asks `has_eligible_recipient` whether to park or
  abandon — an exhausted channel drops the op instead of retrying it for 5 days. An *empty*
  channel is still parked (that backlog is how a new channel reaches its first subscriber).
- **Rate limiting** (`backend/app/core/rate_limit.py`, `app/deps/rate_limit.py`): feed writes
  (create post, forward, drop) share **one per-user budget** — `INTERACTION_RATE_LIMIT` hits per
  sliding `INTERACTION_RATE_WINDOW_SECONDS` window, enforced by a Lua sliding-window log in Redis
  (one round trip, one sorted set per active user, self-expiring). Superusers are exempt; setting
  the limit to 0 disables it. Attach to a route with
  `dependencies=[Depends(limit_interactions)]` — it runs before the handler, so a throttled request
  spends nothing and mutates nothing. Rejections are `429 {"error": "rate_limited", "retry_after": n}`
  plus a `Retry-After` header.
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
