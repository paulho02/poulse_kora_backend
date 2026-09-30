# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

## Project

The product is **Peerkola** (peer + percolation). The repository directories still carry the
pre-rename name (`poulse_kora_backend`, `poulse_kora_app`) and are the one place the old name
is deliberately kept; everything inside them says Peerkola.

FastAPI backend (`backend/`), generated from the `fastapi-starter` template. Its client is the
Flutter app in the sibling repo `poulse_kora_app` (see that repo's CLAUDE.md). Also here:
`landing/` (Astro static site for peerkola.com — see its README), `docs/` (analysis notes and GDPR/hosting research — prose, not
code), `RAILWAY.md` (deployment), `env-template`.

The template's React Admin frontend (`frontend/`) is **not used** and is disabled rather than
deleted: the `frontend` service is commented out in `docker-compose.yml`/`docker-compose.ci.yml`,
as are the frontend build and Cypress steps in `.github/workflows/test.yaml`. Two vestiges remain
and are harmless: `factory.serve_static_app` (404 → `static/index.html` for non-API GETs) and the
React Admin `sort`/`range` + `Content-Range` list convention in `app/deps/request_params.py`, used
only by the template's `items.py`. New endpoints for the mobile app need not follow it.

## Commands

Local dev is Docker Compose (hot reload via `docker-compose.override.yml`).

```bash
docker compose up -d                                   # backend + postgres + redis + minio + landing (:4321)
docker compose up -d --build                           # after changing pyproject.toml
docker compose exec backend alembic upgrade head       # apply migrations
docker compose exec postgres createdb apptest -U postgres   # one-time test DB
docker compose run backend pytest --cov --cov-report term-missing
docker compose run backend pytest tests/api/test_items.py::TestGetItems::test_get_items -v
docker compose exec backend alembic revision --autogenerate -m 'message'   # after editing app/models/
docker compose exec backend alembic check              # model/migration drift (what CI runs)

# Scripts run as MODULES from /app, never as file paths (poetry install --no-root
# only puts /app on sys.path). backend/scripts/README.md lists every script and
# the safe/ vs dangerous/ split.
docker compose exec backend python -m scripts.safe.shell                  # IPython + DB session
docker compose exec backend python -m scripts.dangerous.seed_dev_data     # bot users + posts, dev only
docker compose exec backend python -m scripts.dangerous.bulk_create_posts <channel> <amount> [--language de]
docker compose exec backend python -m scripts.safe.backfill_post_media [--dry-run]   # re-run media pipeline on old videos
docker compose exec backend python -m scripts.dangerous.rebuild_redis     # rebuild Redis state from Postgres (see Language routing)
```

OpenAPI docs: `http://localhost:8000/docs/`. MinIO console: `http://localhost:9001` (log in with
`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` from `.env`). Dependencies via Poetry
(`backend/pyproject.toml`); lint with `ruff` + `isort` through `pre-commit` (`pre-commit install`).

## Architecture

### Plumbing

- **App factory** (`backend/app/factory.py`): `create_app()` wires `api_router` under
  `settings.API_PATH` (`/api/v1`), fastapi-users' auth/register/users routers, CORS,
  `BodySizeLimitMiddleware`, `RequestLoggingMiddleware` (added last, so outermost) and the static
  fallback. Route function names become OpenAPI `operationId`s and must be unique. Every error
  response is normalized to `{"detail": {"error": <code>, ...}}` in `setup_exception_handlers`.
- **Auth** (`app/deps/users.py`): fastapi-users, JWT bearer, typed dependencies `CurrentUser` /
  `CurrentVerifiedUser` / `CurrentSuperuser` / `OptionalUser`. User ids are UUIDs.
  `User.username` is unique and both writers (`POST /auth/register`, fastapi-users' own
  `PATCH /users/me`) answer `409 username_taken`, enforced in `UserManager` because the stock
  routers are where a username reaches the DB. Two layers on purpose: `is_username_taken`
  (`app/core/username.py`) is a probe, not a reservation, so `_conflict_as_api_error` maps the
  unique-index `IntegrityError` (matched by constraint name; anything else stays a 500) to the same
  409 after rolling the session back. A username is `[a-z0-9_]`, `USERNAME_MIN_LENGTH`–`_MAX_LENGTH`
  (`app/core/username_policy.py`): the schemas *normalize* (NFKC, strip, lowercase — `Paul` is
  `paul`, not an error), `UserManager` *refuses* the rest as `400 username_invalid`, and a CHECK
  constraint (`ck_users_username_charset`, alembic `0007`) backstops writers that bypass the
  manager. One case and one script is what makes exact-match uniqueness mean "nobody can register a
  lookalike" — don't widen the charset without a confusables answer. `0007` rewrote pre-existing
  names (clashes get a numeric suffix, oldest account keeps the plain name).
- **DB** (`app/db.py`, `app/deps/db.py`): async SQLAlchemy 2.0 / asyncpg; models subclass `Base`;
  one session per request via `CurrentAsyncSession`.
- **Config** (`app/core/config.py`): pydantic-settings from `.env`. **The comments in that file are
  the reference for every knob** — sections below only cover what spans modules. Under pytest,
  `DATABASE_URL`, `REDIS_URL`, `S3_BUCKET_NAME` and `S3_PUBLIC_ENDPOINT_URL` are swapped for their
  `TEST_*` counterparts, so tests never touch dev data. Validators guarding a secret are *field*
  validators, never model validators: pydantic quotes the validated input in its error, and a model
  validator's input is the whole settings dict including `SECRET_KEY`. Startup refuses a short or
  placeholder `SECRET_KEY` (`openssl rand -hex 32`), `EMAIL_PROVIDER=lettermint` without a token,
  and a malformed `CONTENT_LANGUAGES`. Unknown keys in `.env` fail too (e.g. `FEED_SEEN_TTL_SECONDS`,
  which is a derived `@property`).
- **Migrations** (`backend/alembic/versions/`): CI runs `alembic upgrade head` then `alembic check`,
  so every model change needs a migration. History was squashed to `0001_initial_schema` +
  `0002_seed_channels` before launch; `0003`+ are the real path forward. **Don't squash again** —
  each revision is now the only route a live database has. A database whose `alembic_version`
  names a vanished revision is fixed with `python -m scripts.dangerous.reset_database`, not a manual
  stamp. Some migrations need a Redis step afterwards (noted per feature below).
- **Tests** (`backend/tests/`): pytest-asyncio auto mode; session-scoped `httpx.AsyncClient` from
  `create_app()` in `conftest.py`. `auto_rollback` rolls back per test, but `create_user`/
  `create_item` are session-scoped factories, so isolation is best-effort. `tests/utils.py::
  get_jwt_header(user)` authenticates without the login route. `conftest.py`'s `no_outbound_email`
  pins the email connector per test — `EMAIL_PROVIDER` has no `TEST_` counterpart, so without it
  a dev with `EMAIL_PROVIDER=lettermint` in `.env` would have every registration test hit the real
  provider. The `httpx` pin has a real ceiling: lettermint needs `>=0.27`, and `0.28` removed the
  `AsyncClient(app=...)` shortcut the test client uses.
- **Logging** (`app/core/logger.py`, `request_logging.py`): one stdout handler, configured in
  `create_app()` (after uvicorn installs its own, which is reclaimed). `LOG_FORMAT=auto` → console
  locally, JSON on Railway, where each top-level key is a filterable attribute (hence flat
  payloads). Rules:
  - Event names, not prose: `log.info("post.created", post_id=..., price=...)`; `get_logger(__name__)`
    returns an adapter accepting keywords.
  - The request id is the join: the middleware opens a contextvar (id, method, path, client IP),
    auth deps bind `user_id`, a filter stamps all of it on every record. Returned in
    `X-Request-ID` and in a 500's `detail.request_id`. The worker opens the same context per op.
  - Errors are logged once, in the middleware (`http.request_failed`). `_unhandled_exception_handler`
    runs outside it, logs nothing and reads the id off `scope`.
  - INFO scales with traffic, not work: per-retry/per-item/per-poll is DEBUG (`feed.op_parked`
    above all — a parked op re-parks every 20s for up to 10 days). `LOG_QUIET_PATHS` demotes
    `/health` and `/posts/feed/status`; `LOG_SLOW_REQUEST_MS` promotes; `LOG_LEVEL_OVERRIDES`
    turns one module up on a live deploy.
  - Ids, never contents — no emails, post text, tokens or query strings. The one exception is
    `send_email`'s unconfigured-SMTP branch, which *is* dev delivery. `LOG_CLIENT_IP` is a GDPR
    flag; *where* the IP comes from is derived, not configured (`caller_address`: rightmost
    `X-Forwarded-For` on Railway, socket peer otherwise — rightmost because a proxy appends, so the
    leftmost entry is the caller's to forge). RAILWAY.md has queries worth saving; `SENTRY_DSN` is
    still unwired.
- **Localization**: `SUPPORTED_LOCALES`/`DEFAULT_LOCALE`; locale resolved per request from
  `Accept-Language` (`app/core/locale.py`, `CurrentLocale`). Deliberately no persisted `User.locale`
  — the pre-login banner has no user. Not to be confused with `CONTENT_LANGUAGES` (the language a
  post is *written in*); the two lists may diverge. **User-facing strings are never hardcoded
  prose**: routes raise `api_error(status, "some_code")` (`app/core/errors.py`) and the Flutter
  `.arb` files supply the copy. The only free text generated here: `password_policy.py` (structured
  `[{code, params}]` violations — add codes, not sentences), `banner.py` (admin-authored, one
  message per locale, resolved server-side) and `email_templates.py`.
- **Rate limiting** (`app/core/rate_limit.py`, `app/deps/rate_limit.py`): Lua sliding-window log
  in Redis; `enforce` is the one place a `429 {"error": "rate_limited", "retry_after": n}` +
  `Retry-After` is built. Attach with `dependencies=[Depends(limit_x)]` — it runs before the
  handler, so a throttled request spends nothing. Every budget is disabled by a limit of 0.
  - `limit_interactions`: create/forward/drop share one per-user budget so alternating routes
    can't dodge it; superusers exempt.
  - `limit_login`: two budgets per attempt — per caller address (bounds argon2 CPU per host) and
    per account (hash of the normalized email, so no address enters Redis or a log). Router-level on
    fastapi-users' auth router; reads the email off the parsed form rather than declaring
    `OAuth2PasswordRequestForm`, so `/logout` on the same router isn't made to demand login fields.
  - `limit_register` per address; email change per account inside `UserManager._update` — both
    send mail to an address the caller chose.
  - `limit_feedback`: user id when there is one, client IP otherwise (the only IP-keyed budget).
  - Change-password and account export have their own budgets (see those sections).
  The address identity is `caller_address` in `request_logging.py`, the same derivation as the log
  line: keyed on the socket peer behind Railway's edge, every anonymous caller shares one budget;
  keyed on the leftmost forwarded entry, the budget is the caller's to reset.
- **Request body limits** (`app/core/body_limit.py`): nothing in uvicorn/Starlette/FastAPI bounds
  a body, and it is read (multipart spooled to disk, JSON whole) *before* any dependency runs.
  `BodySizeLimitMiddleware` checks `Content-Length` and counts `receive` bytes against
  `MAX_UPLOAD_BODY_BYTES` (multipart — must stay above the largest upload total plus framing) or
  `MAX_REQUEST_BODY_BYTES` (everything else). By content type rather than a route list, because a
  path list goes stale silently. `413 request_too_large`.

### Email

**Outbound email** (`app/core/email.py`, `email_templates.py`): `send_email(to, subject, body,
html=None)` is the only entry point; `EMAIL_PROVIDER` picks `smtp` (default; with `SMTP_HOST`
unset it *logs* the mail, which is how a dev reads a verification code out of `docker compose
logs`) or `lettermint` (EU provider, official SDK). Only SMTP may degrade to logging: a named
connector with no token fails at startup, because degrading would print verification codes into a
production log and fail silently until a user reported no code arriving. Lettermint specifics: a
**client per send** (the SDK caches a mutable email builder on the client, so a shared one would
interleave two coroutines' recipients); its exceptions stringify to nothing, so the connector logs
`.response_body` itself (`email.lettermint_rejected`, recipient scrubbed); `LETTERMINT_FROM_*` fall
back to `SMTP_FROM_*` and exist only because a from-address is provider-scoped.

Templates: `body` is never optional (HTML-only is a spam signal, and a text-preferring client
would show nothing). Markup is 2005-style — nested tables, inline styles, no external assets —
because Gmail strips `<style>`, Outlook renders through Word, and remote images are blocked by
default; the code must stay selectable text. `tests/core/test_email_templates.py` pins each rule,
since each fails silently in one popular client. Copy is localized here (no client in an inbox),
one dict per locale, `_t` falling back per key so a half-translated locale sends English rather
than failing. fastapi-users' `on_after_register`/`on_after_update` hooks get only a `Request`, so
`_locale_of()` in `deps/users.py` resolves the locale there, falling back to `DEFAULT_LOCALE`.

### Accounts

- **Google sign-in** (`app/api/google_auth.py`, `app/core/google_oauth.py`): an ID-token flow —
  the client obtains the token, `POST /auth/google` verifies it and mints our JWT — not
  fastapi-users' redirect `get_oauth_router`, which the mobile app has no deep links for and whose
  email linking is silent. Still uses fastapi-users' `oauth_account` table and callbacks. Behind
  `GOOGLE_OAUTH_ENABLED` + `GOOGLE_CLIENT_IDS`, advertised on `GET /config`.
  - `email_verified` must stay checked: linking matches on email, so an unverified claim is an
    account-takeover path.
  - Linking is one-way: `hashed_password` becomes a random value; `auth_provider="google"` makes
    `authenticate` answer `login_use_google` and change-password `google_account_no_password`.
  - Email is a contact address, not a credential. Identity is the Google `sub`, one account per
    `sub` (`google_account_in_use`). `POST /auth/google/link` accepts a Google account whose address
    differs and leaves `User.email` alone; `is_verified` is set only when the addresses match. It
    requires `current_password` (`google_link_password_required`/`google_link_wrong_password`,
    change-password budget), checked before the Google token: it destroys the password, so a
    stolen token alone would otherwise lock the owner out for good. The
    same rule runs the other way: changing `email` via `PATCH /users/me` revokes `is_verified` and
    mails a fresh code (`UserManager._update`).
  - A password account with a matching address gets `409 google_link_required`; the client
    re-sends the same token with `link_existing`. No server state between the two — Google tokens
    live about an hour.
  - `User.oauth_accounts` is `lazy="selectin"`, not the `joined` the docs show: a joined
    collection would force `.unique()` on every `select(User)` in the codebase.
- **Profile pictures** (`app/api/users.py`): `PUT`/`DELETE /users/me/profile-picture`, keyed by
  `User.profile_picture_key`. Bytes go through `media_validation.process_profile_picture` (decode,
  EXIF strip, downscale, re-encode — content type derived, not believed). No GET route:
  `UserRead`/`PostAuthor` carry `profile_picture_url`, a presigned link, populated only in
  `_serialize_post`'s `reveal_author` branch. Every upload writes a new key, so a replaced picture
  gets a new URL and client caches invalidate themselves; the old object is deleted after commit.
- **Account deletion** (`DELETE /users/me`, `app/core/account_deletion.py`): irreversible; the
  user chooses **keep posts** (nulls `posts.author_id`; `_serialize_post` folds that into its
  anonymous branch) or **erase them** (rows + bucket objects). No soft delete, tombstone or grace
  period — each keeps what this exists to destroy.
  - Current password required, as for `POST /auth/change-password`, sharing its rate-limit
    budget; a Google account (random hash nobody holds) is exempt.
  - Explicit bulk statements, never `session.delete(user)`: the ORM cascade on `User.posts` would
    erase posts on the way to the user row. Order is fixed by hand (blocks before `post_media`,
    children before parents). `feedback.user_id` is the one FK left to the schema
    (`ON DELETE SET NULL`), so a bug report outlives its reporter.
  - Postgres commits before anything irreversible outside it: bucket deletes last, Redis purge
    after commit.
  - Erased posts are announced into Redis (`service.mark_posts_deleted` → `deleted:{id}`, TTL
    `FEED_RETRY_MAX_AGE_SECONDS`) because the worker has no Postgres session; `process_operation`
    retires those ops (`reason="post_deleted"`).
  - Queues are left alone (finding delivered copies means scanning every reader's queue); the
    feed answers the hole instead (see "A queue slot can outlive its post").
  `backfill_queue` filters authorless posts with `is_distinct_from` — `NULL != <uuid>` is NULL.
- **Data export** (`GET /users/me/export`, `app/core/account_export.py`,
  `schemas/account_export.py`): GDPR Art. 15/20 — a streamed ZIP of `README.txt`, `data.json` and
  every uploaded object.
  - Files, not presigned links: SigV4 caps `X-Amz-Expires` at 7 days, a presigned URL is its own
    authorization, and the data must survive a `DELETE /users/me` right after.
  - Everything is read in the route, nothing during the stream: FastAPI closes `yield`
    dependencies before a `StreamingResponse` body is consumed, so `collect` returns plain Python
    and `stream_zip` touches only `app.core.storage` (process-lived client). A lazy generator
    passes the test and fails on a real server.
  - Built into a write-only `_Sink` so `zipfile` emits data descriptors (the only streamable
    mode); media `ZIP_STORED`; peak memory is one object plus the JSON.
  - An unreadable object doesn't fail the export (the 200 is already out); it's listed in
    `media/UNAVAILABLE.txt`.
  - The JSON is an explicit pydantic allow-list: a field earns its place by telling the reader
    something about themselves. Omissions are named in `export.omitted` as stable codes
    (`credentials`, `internal_flags`, `transient_queue_state`, `post_vote_counts`,
    `other_peoples_posts`); media entries carry archive path and kind, not what the file itself
    answers; names over internal ids (post ids stay). `tests/api/test_account_export.py::
    TestExportOmissions` fails when a new `User` column leaks in. Serialized `exclude_none`.
  No password (a token can already read all of this one route at a time, and a Google account
  has none). Own budget `ACCOUNT_EXPORT_RATE_LIMIT` (3/24h). `SUPPORT_EMAIL` is printed in the
  README.
- **Supporter subscription** (`app/api/subscriptions.py`, `app/core/mollie.py`): Mollie-backed,
  shipped dark behind `SUBSCRIPTIONS_ENABLED` — every route 404s except the webhook, so an
  in-flight payment isn't stranded by a toggle. `scripts.safe.grant_subscription` grants an
  entitlement by hand. `config.py`'s `MOLLIE_*`/`SUPPORTER_*` comments cover the rest.

### Media and storage

- **Object storage** (`app/core/storage.py`, `sigv4.py`): every image, video and poster lives in
  an S3-compatible bucket — MinIO locally/CI, a Railway Bucket (Tigris) in production; only the
  `AWS_*`/`S3_*` settings differ (RAILWAY.md, `env-template`).
  - The variable names are the S3 convention (`AWS_ENDPOINT_URL`, `S3_BUCKET_NAME`,
    `AWS_DEFAULT_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`) so Railway's bucket
    auto-connect and boto3/aws/rclone work with no mapping. The `S3_`-prefixed extras
    (`S3_ADDRESSING_STYLE`, `S3_PUBLIC_ENDPOINT_URL`, `S3_AUTO_CREATE_BUCKET`, `TEST_S3_*`) describe
    how we address the bucket. `S3_ADDRESSING_STYLE=virtual` is the one Railway setting
    auto-connect can't supply; a wrong value fails as `SignatureDoesNotMatch`.
  - Clients never hold a credential; they get a **presigned URL** minted after the authorization
    check, valid `MEDIA_URL_TTL_SECONDS` even if access is revoked meanwhile. Keep the TTL modest.
  - Object keys are flat random UUIDs, never author-derived — an anonymous post's URL is shown
    to everyone.
  - Presigning is quantized to `MEDIA_URL_REFRESH_SECONDS`, so one object yields a byte-identical
    URL within a window; the Flutter client caches by URL. This is why SigV4 is implemented here
    rather than via boto3, which stamps wall-clock time with no way to pin it.
  - Hand-rolling SigV4 means hand-rolling the **retry** boto3 would have brought, because nothing
    underneath backs off on our behalf. `_request` re-attempts a transient answer —
    503 (S3 spells throttling `SlowDown`), 500/502/504, 429, and a transport error —
    `STORAGE_MAX_ATTEMPTS` times with jittered backoff, and never retries a 4xx, which is
    deterministic. **The signature is recomputed inside the loop**: SigV4 covers `x-amz-date`, so
    replaying the first attempt's headers turns a retryable 503 into a permanent
    `SignatureDoesNotMatch` — the error only changes its face. Keep the budget small; the upload
    runs *before* the post is committed, so it is latency the author waits through. This is not
    theoretical: Tigris answered `SlowDown` on a single image PUT in production, and because
    `_store_media` is all-or-nothing that lost the whole post (no token spent — Postgres had not
    been touched). `tests/core/test_storage.py::TestTransientRetry` pins which answers are
    replayed and which are not.
  - The host is a signed header, so a URL signed for `minio:9000` cannot be rewritten to
    `localhost:9000` — hence `S3_PUBLIC_ENDPOINT_URL` beside `AWS_ENDPOINT_URL`, the published port
    in `docker-compose.override.yml`, and bucket CORS (`MINIO_API_CORS_ALLOW_ORIGIN`) for Flutter
    web.
- **Post media** (`app/core/media_validation.py`, `models/post_media.py`): `process_upload` is the
  choke point — nothing reaches storage or spends a token before it. Uploads happen before commit,
  so a storage failure aborts the post; the residual failure is an orphaned object.
  - Two fixed ratios, `POST_MEDIA_LANDSCAPE_RATIO` (4:3) and `POST_MEDIA_PORTRAIT_RATIO` (4:5).
    Images are cropped in the client (the only place that can show the author what's lost) and
    only *validated* here — `400 post_media_invalid_aspect_ratio`, never a silent crop. A video
    can't be re-encoded in Flutter, so the client sends `PostBlockIn.orientation` and the center
    crop happens inside the H.264/AAC transcode (`nearest_orientation` if omitted).
    `PostMedia.width/height` are nullable; null means "unknown, letterbox it".
    `scripts.safe.backfill_post_media` re-runs the pipeline over rows that predate it.
  - Every video gets a poster frame (`poster_object_key`), taken from the transcoded output a
    little after frame 0 (routinely a black fade-in), behind the same view gate as the clip.
    Extraction is non-fatal — `poster_url` is nullable.
  - No byte-serving routes: `Range` requests (how a player scrubs) go to the bucket.
  - EXIF is *applied* (`exif_transpose`) before it is stripped, or a phone portrait — a landscape
    frame plus a rotate tag — is stored sideways and measured against the wrong ratio.
  - Hardening that is easy to undo: `_strip_info` runs before every save (Pillow's encoders fall
    back to `im.info["comment"]`/`["icc_profile"]`, and `convert`/`exif_transpose`/`thumbnail` copy
    `info` across); ffmpeg is told the input format (`-f mov`) rather than sniffing it;
    `-map_metadata:s:v -1 -map_metadata:s:a -1` join the global flag, which leaves per-stream tags;
    every ffmpeg/ffprobe child runs under `_FFMPEG_SLOTS` (`MEDIA_MAX_CONCURRENT_FFMPEG`,
    `MEDIA_TRANSCODE_THREADS`); `_open_within_pixel_budget` refuses an oversized image from the
    header alone rather than relying on Pillow's warn-then-refuse-at-2x, which a warnings filter
    can reset.
- **Feedback** (`app/api/feedback.py`, `models/feedback.py`): in-app feedback/bug reports with
  attachments. Works signed out (`OptionalUser`) because "I can't sign in" is otherwise
  unreportable.
  - The route doesn't trust the form: with no user, `is_anonymous` is forced on and
    `allow_contact` off; an expired token degrades to anonymous rather than a rejection.
  - Anonymity is the absence of the link: no `user_id` is stored at all (unlike `Post.author_id`,
    which fan-out needs). `is_anonymous` only marks a NULL id as a choice.
  - No contact address is stored: `contact_email` is a property off `user` (hence
    `lazy="selectin"`), and `user_id` is `ON DELETE SET NULL`, so erasure cuts the link.
  - `locale` is a column — anonymous rows have no user to read one from.
  - `status` (`FEEDBACK_STATUSES`) is triage state nothing reads yet; a plain string, read-only
    on the superuser listing. `feedback_create_form` is an allow-list, so `status`, `user_id`,
    `locale`, `created` and `user_agreed_data_saving_at` are unreachable from a request.
  - `user_agreed_data_saving_at` is server-stamped and NOT NULL; `consent=true` is required
    (`feedback_consent_required`), so a row existing is the evidence.
  - `process_feedback_upload` exists beside `process_upload` because the post ratios must not
    apply (a screenshot is whatever shape the screen is; PNG stays PNG). EXIF is still stripped.
  Read back only via superuser `GET /feedback`.

### Feed economy

- **Language routing** (`app/core/languages.py`, `app/feed/keys.py`): a post is written in one
  language, a reader accepts a set, fan-out delivers where the two meet. The audience of an op is
  `keys.audience(channel_id, language)` — a Redis set that already exists before the post does,
  so `select_recipients` is still one `SRANDMEMBER`. Subscribing writes one membership per
  (channel × accepted language); the cost sits in a rare write, not every fan-out. Post-sample
  filtering was rejected: with a sample of 12, a language read by a tenth of a channel yields ~1
  candidate and nearly every op would park. **Rule for any new axis**: into the key if it
  partitions strongly and has a small closed value set; into a post-sample `SMISMEMBER` if it
  merely trims; freeform tags in neither.
  - `LANGUAGE_UNSPECIFIED` (`"und"`) routes through the plain `channel:{id}` set — already the
    union of the language slices — so it needs no set of its own, and the plain set still serves
    the subscriber count and what `sync_unsubscribe`/`purge_user` clear. Accepted only for a post
    with no text blocks (the one checkable part); a text-free post may still declare a language.
  - Cheap-and-universal is not an arbitrage: forwarding is free, so a mislabelled post buys cheap
    reach to people who drop it — and it's what keeps a minority-language reader's feed from being
    structurally empty. The residual hole (text baked into an image) is a metric to watch (drop
    rate of UNSPECIFIED vs tagged posts), not a mechanism.
  - `has_eligible_recipient` measures the route, never the channel — otherwise a German post
    whose German readers are exhausted would see thousands of English subscribers and park for ten
    days. Exhaustion is ordinary: `feed.op_abandoned` with `reason="route_exhausted"` is the normal
    end of a post's life.
  - Language is self-declared; the client detects on-device and prefills, the author can
    override, the forward/drop economy corrects.
  - Ops degrade: a stream or `ops:retry` entry without a language reads as UNSPECIFIED, so a
    backlog crossing the deploy delivers as minted.
  - `User.content_languages` is a never-empty Postgres array with its own route
    (`PUT /users/me/content-languages`) writing the **absolute** set, because the Redis
    memberships are the other half and fastapi-users' `PATCH` knows nothing about them. New
    accounts are narrowed to their request locale in `on_after_register`; the column default is
    every language, so rows created outside registration keep their feed.
  **Deploying this — and adding a `CONTENT_LANGUAGES` entry — is two steps**: `alembic upgrade
  head`, then `python -m scripts.dangerous.rebuild_redis` to create the per-language audience
  sets. Between them a tagged post finds an empty audience and parks.
- **Delivery exclusions** (`app/feed/`): `FEED_EXCLUDE_OWN_POSTS` carries `author_id` on the
  stream entry (no stored state). `FEED_EXCLUDE_SEEN` keeps `seen:{post_id}` sets written inside
  the `place` Lua script, so a recipient is recorded atomically with delivery. `select_recipients`
  filters on both (efficiency); `place_post` re-checks and returns `PLACE_REFUSED` (correctness) —
  never count a refusal as a delivery. Postgres' unique `(user, post)` review constraint is the
  backstop. **Enabling `FEED_EXCLUDE_SEEN` on an existing DB requires `rebuild_redis`.**
  Consequence: saturation is reachable, so `process_operation` asks `has_eligible_recipient`
  whether to park or abandon — an exhausted route is dropped, an *empty* channel still parks (that
  backlog is how a new channel reaches its first subscriber). `FEED_RETRY_MAX_AGE_SECONDS`
  (10 days) is deliberately generous; `FEED_SEEN_TTL_SECONDS` is a derived `@property`
  (`FEED_SEEN_TTL_RETRY_MULTIPLE` × it), not a setting — its docstring in `config.py` explains why
  a multiple and why getting the pair wrong fails silently.
- **Keeping the feed current** (`GET /posts/feed`, `GET /posts/feed/status`): the queue is pushed
  into by the worker, so a client that fetches once only ever sees it shrink. `/feed/status` is the
  cheap poll — one `LRANGE`, no Postgres, no presigning — answering with the queue's **post ids**
  (not a count: with a channel filter, a count makes every tick look like news) plus `capacity`
  (`FEED_QUEUE_MAX_SLOTS`), since a full queue is a different instruction from an empty one. It
  marks the user active, as a feed read does. `/feed`'s `limit` defaults to `FEED_QUEUE_MAX_SLOTS`
  (a client omitting it always holds the whole queue), and `channel_id` is filtered in SQL before
  `skip`/`limit`. Deliberately **no pull-based top-up**: undelivered supply exists, but reach is
  what an author paid for (`FEED_FANOUT` per op), so serving it on demand would be an economy
  change. A feed that runs dry is the economy working.
- **A forward can gift its token** (`PostReviewCreate.gift_token`, forward only — 422 on a drop):
  the token the review earns goes to the post's author instead of the reviewer. A transfer, not a
  mint, so supply is unchanged and it can't be farmed; it buys the author voice (their next post's
  admission price). Recorded as `PostReview.gifted` (alembic `0008`) because `rebuild_redis`
  re-derives balances from Postgres and must move each gift from giver to author. Refused
  `409 gift_not_allowed` — before the queue is touched, so the post stays reviewable — for a probe,
  one's own post, or an author who deleted their account. Anonymity holds: the reviewer learns
  nothing about who received it. `PostReviewResult.gifted`, `ReviewedPostRead.gifted` and the
  export's `gifted_token` report it back. The receiving side is `Post.gifted_count` (alembic `0009`,
  backfilled from `post_reviews.gifted`), incremented SQL-side like `forwarded_count` and the one
  per-post count on a read route: `PostRead.gifted_count` is set **for the author only** (null for
  every other viewer, superusers included) — it is their income, not a verdict to vote along with.
  Exported as `gifted_tokens` on the author's posts for the same reason.
- **A post's score is disclosed once, after the verdict**: `POST /posts/{id}/review` returns
  `post_forwarded_count`/`post_reviewed_count` (including this review). `Post.forwarded_count`/
  `dropped_count` are deliberately absent from `PostRead` — a reader who can see the crowd's vote
  votes on the crowd, and hiding it client-side leaves the API as the way around. Counters are
  incremented SQL-side (`Post.forwarded_count + 1`; `FEED_FANOUT` readers contend on the row),
  which expires the attributes — hence the explicit `session.refresh(post, [...])` after commit.
  Reviewers, not viewers: there is no delivery counter.
- **A queue slot can outlive its post**: `GET /posts/feed` returns `list[FeedEntry]`
  (`{post_id, post}`), `post` null for a slot whose row is gone — an envelope, not a nullable
  `PostRead`, because a vanished post has no channel/author/time to invent. Holes are reported
  only in the unfiltered view (under a channel filter, "not in this channel" and "gone" are
  indistinguishable). `DELETE /posts/feed/{post_id}` reclaims the slot: not a review (nothing read,
  nothing earned), `409 post_available` while the post exists so it can't skip one, and outside the
  interaction budget — a queue of ghosts is up to `FEED_QUEUE_MAX_SLOTS` of them.
- **Reviewer trust** (`app/core/trust.py` formula, `trust_service.py` inputs, `probes.py` test
  posts): how carefully a reader reads scales how far their *forwards* travel (⅔ / 1 / ⁴⁄₃ ×
  `FEED_FANOUT` → 2/3/4). It never touches an original post — that reach was paid for; a posting
  discount would be creator trust, which doesn't exist. Safe because forwarding is free.
  `config.py`'s `TRUST_*` comments document every knob; the structural rules:
  - Everything is a trailing `TRUST_WINDOW_DAYS` (30) window, nothing lifetime — that *is* the
    inactivity rule; no decay coefficient, no last-active column.
  - Absence of evidence is never evidence: each component shrinks toward neutral by its own
    confidence, so a new account scores exactly 50.
  - Anomaly is a ceiling on the above-neutral half (`compute_score`), never a penalty — only
    failed probes can cost reach, which makes "dropping a run of bad posts is allowed" arithmetic
    rather than tuning. Volume alone tops out at 65, below the high band. Tests pin both.
  - The forward-rate signal compares against the deployment's rate (cached; skipped below
    `TRUST_FORWARD_RATE_MIN_SAMPLE`), normalized per side — so blind forwarding is caught far harder
    than blind dropping, which only wastes the dropper's time.
  - Probes are real `Post` rows (`Post.is_probe`), minted on the review path (never in the worker,
    which has no Postgres session) and placed with the ordinary `place_post`. No fan-out op is
    minted, so they never move the admission price. Answering one earns a token and nothing else —
    no `PostReview` row, no counters — so `reviewed_count == forwarded + dropped ==
    COUNT(post_reviews)` holds and probes stay out of the signals they calibrate.
  - The score is lazy and cached (`TRUST_CACHE_TTL_SECONDS`): two indexed aggregates (`lag()` over
    `ix_post_reviews_user_created`, plus `probe_responses`), invalidated on a probe answer.
  - The probe author is minted by migration `0005_probe_author` so its email/username can't be
    squatted before first use. `_ensure_probe_author` still creates on demand (the test schema is
    `create_all`) but only adopts an `is_active = false` row; its name is reserved in
    `_check_username_free`, and `TRUST_PROBE_AUTHOR_USERNAME` is validated against the username
    rule at startup (the CHECK constraint would otherwise refuse the insert on the first probe). `0006_rename_probe_author` moved that identity to the Peerkola one on
    databases already past 0005 — renaming those two settings again needs the same treatment,
    or the old row is orphaned and a second author is minted.
  - **No cleanup job for probe rows**: the score reads `ProbeResponse.created` (answered), a probe
    carries `Post.created` (minted), and a probe waits in a Redis queue Postgres can't see —
    pruning by mint age would move live trust and hand readers ghost cards.
  Residual hole: probes are marked (`PostRead.is_probe`) because measuring people secretly is a
  trick, so a patient adversary can ace probes and blind-drop ~95% of the rest. The signature
  (near-perfect probe accuracy beside a near-zero forward rate) is one query; watch it, don't
  mechanise it. Deploying is `alembic upgrade head` only — `trust:*` keys are caches.
  `TRUST_ENABLED=false` restores flat fan-out but keeps the display.
- **Admission pricing is per route** (`app/feed/pricing.py`, `service.route_prices`): a channel
  is several routes (one per content language plus the no-language one), each scaled from the
  shared base price by its own congestion. `ChannelRead` carries `post_price_min/max`,
  `GET /posts/economy` the deployment-wide range, `GET /posts/price?channel_id=&language=` the
  exact charge — a quote guaranteed until `expires_at`, because it and `create_post` read the same
  cached route price.
  - Congestion pricing, not compensation for audience size: tokens buy `FEED_FANOUT` deliveries
    on any route. `route_factor` protects a small route's queue.
  - The range is observed, not enumerated: every route price computed for a real request widens
    `keys.PRICE_RANGE` (Lua-guarded min/max per window), and `GET /channels` prices every route it
    lists, so the range converges on the first channel-list load of each window at no background
    cost. A window with no observations carries the previous spread across, rescaled by the
    base-price move (`_rescale_range`, which also widens to include the current base). Collapsing
    to base-at-both-ends instead was a 60-second-period bug: the composer showed one number, lower
    than the charge. `read_price_range` returns None only where nothing was ever observed.
  - `FEED_PRICE_CHANNEL_BAND` is 0.5 (0.25 rounded the whole spread down to one or two tokens).
    It is an economy lever, not a display knob.
  `keys.SUBS_TOTAL` counts **audience memberships** (channel set plus one per accepted language),
  not subscriptions, because `route_factor` divides a route's audience by it.
