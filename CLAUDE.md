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

# Bulk-create N test posts in a channel (by ID or name), authored by an
# auto-created superuser bot. Calls the real create_post route function
# directly, so it always reflects actual post creation behavior.
docker compose exec backend python bulk_create_posts.py <channel> <amount>
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
- **Google sign-in** (`backend/app/api/google_auth.py`): an **ID-token** flow, not fastapi-users'
  `get_oauth_router` — that is a browser redirect flow the mobile app has no deep links for, and
  its `associate_by_email` linking is silent, leaving nowhere for the confirmation step. The client
  gets a Google ID token itself, `POST /auth/google` verifies it (`app/core/google_oauth.py`, via
  Google's `google-auth`) and mints our normal JWT. Underneath it is still fastapi-users' own
  machinery: the `oauth_account` table, `SQLAlchemyUserDatabase(session, User, OAuthAccount)` and
  `oauth_callback` / `oauth_associate_callback`. Behind `GOOGLE_OAUTH_ENABLED` +
  `GOOGLE_CLIENT_IDS`, advertised to the client on `GET /config`.
  **`email_verified` is checked and must stay checked** — linking matches on email, so accepting an
  unverified claim would be an account-takeover path against every password account.
  Account identity is **one-way**: linking overwrites `hashed_password` with a random value, so
  `User.auth_provider` flipping to `"google"` is what makes `authenticate` answer `login_use_google`
  and `POST /auth/change-password` answer `google_account_no_password` (both in `app/deps/users.py`).
  **Email is a contact address, not a credential.** An account is bound to a Google identity by
  `sub`, so `POST /auth/google/link` deliberately accepts a Google account whose address differs
  from `User.email` and leaves that column alone — the one invariant it defends is that a `sub`
  maps to at most one account (`google_account_in_use`), since two would leave a later sign-in
  unable to tell which was meant. Consequences: `PATCH /users/me` may still change a Google
  account's email (there is no lock, by design), and linking only sets `is_verified` when the two
  addresses match — Google vouched for *its* address, and flipping the flag for a different one
  would be a free pass around email verification. The same rule runs the other way in
  `UserManager._update`: **changing `email` revokes `is_verified`** and mails a fresh code
  (`on_after_update`), because the flag is proof about an address, not about an account.
  A password account whose address matches gets 409 `google_link_required` on the first attempt and
  is only linked when the client re-sends the *same* token with `link_existing` — no server state
  between the two, since Google ID tokens live about an hour. `User.oauth_accounts` is
  `lazy="selectin"`, deliberately not the `joined` fastapi-users' docs show: a joined *collection*
  eager load obliges every `select(User)` in the codebase to call `.unique()` or raise at runtime.
- **Profile pictures** (`app/api/users.py`): stored **as bytes in Postgres** (`User.profile_picture`
  + `profile_picture_content_type`) because there is no file storage yet — a deliberate stopgap, not
  a pattern to copy. Set via `PUT /users/me/profile-picture` (multipart, validated against
  `PROFILE_PICTURE_MAX_BYTES` / `PROFILE_PICTURE_ALLOWED_CONTENT_TYPES`), cleared via `DELETE`.
  What `UserRead`/`PostAuthor` expose is **`profile_picture_url`, never the bytes** — a feed lists
  many posts, often several by one author, so embedding base64 would repeat the whole image on every
  one; the URL points at `GET /users/{id}/profile-picture`, which the client fetches and caches once
  per author. That route **requires auth**, so clients cannot treat it as a plain image URL.
  The anonymity rule needs no new code: `_serialize_post` populates the field only inside its
  existing `reveal_author` branch, so an anonymous post withholds the picture along with the id and
  username. Note the URL is derived from the user id and so is *unchanged* when a picture is
  replaced — clients must evict their own cache on upload rather than diffing the string.
- **Post media** (`app/core/media_validation.py`, `app/models/post_media.py`): images and
  videos attached to a post, stored in-DB as bytes like profile pictures and for the same
  stopgap reason. `process_upload` is the choke point — nothing reaches Postgres or spends a
  token before it. Three things are load-bearing:
  - **Two fixed aspect ratios**, `POST_MEDIA_LANDSCAPE_RATIO` (4:3) and
    `POST_MEDIA_PORTRAIT_RATIO` (4:5), and the two media kinds reach them by opposite
    routes. An **image** is cropped in the Flutter client — the only place that can show
    an author what the crop discards — and merely *validated* here, so a wrong shape is
    `400 post_media_invalid_aspect_ratio`, never a silent server-side crop. A **video**
    cannot be re-encoded in a Flutter client at all, so the client sends only a
    `PostBlockIn.orientation` and the center crop happens in the transcode that was
    already running (falling back to `nearest_orientation` when omitted). Consequence:
    `PostMedia.width/height` are nullable and rows predating this are *not* backfilled and
    may be any shape — a client must treat missing dimensions as "unknown, letterbox it".
  - **Every video carries a poster frame** (`PostMedia.poster`, served at
    `GET /posts/{id}/media/{id}/poster` behind the same view gate as the clip, since a
    poster is a frame *of* it). Taken from the transcoded output, so it is cropped and
    scaled identically, and from a moment slightly in rather than frame 0, which is
    routinely a black fade-in. Extraction is deliberately **non-fatal** — a clip that
    transcodes but yields no frame is still a good clip, so `poster_url` is nullable and
    the client falls back to a neutral tile.
  - `PostMedia.data` and `.poster` are **deferred columns**. A feed response serializes
    many posts and wants only metadata; undeferred, one `GET /posts/feed` dragged every
    attached clip's bytes through the ORM to throw them away. The two byte-serving routes
    undefer explicitly, and must pass `populate_existing=True` — `_get_post_with_relations`
    has already put the row in the identity map with the column still deferred, and
    `session.get` returns that cached instance without applying options, so the attribute
    access would emit a lazy load and raise under asyncio.
  EXIF is *applied* (`ImageOps.exif_transpose`) before it is stripped: a phone "portrait"
  photo is often a landscape sensor frame plus a rotate-90 tag, which would otherwise be
  stored sideways and measured against the wrong ratio.
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
- **Localization**: `SUPPORTED_LOCALES`/`DEFAULT_LOCALE` in `app/core/config.py` (English + German
  today). Locale is resolved per-request from `Accept-Language` (`app/core/locale.py`,
  `app/deps/locale.py`'s `CurrentLocale` dependency) — deliberately no persisted `User.locale`
  column, since the pre-login banner endpoint has no user yet. **New user-facing backend strings
  must not be hardcoded English prose** — almost everything already follows the `api_error(status,
  "some_code")` contract (`app/core/errors.py`): the code is stable, and the Flutter client's
  `lib/l10n/*.arb` supplies the actual copy for it (see that repo's CLAUDE.md). Only two things on
  this side generate real free text: `app/core/password_policy.py` (returns a structured
  `[{code, params}, ...]` violation list, never prose — extend with more codes if you add a rule,
  don't return a sentence) and `app/core/banner.py` (admin-authored content with no fixed code set,
  stored as one message per locale and resolved server-side to `Accept-Language` — follow this
  per-locale-dict pattern for any similar free-text-from-an-admin feature, not a single string).
