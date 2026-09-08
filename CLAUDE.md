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

# Re-run the current media pipeline over videos stored before poster frames,
# dimensions and the H.264 transcode existed - those render as a black
# rectangle in the client. Idempotent; --dry-run just counts.
docker compose exec backend python backfill_post_media.py [--dry-run]

# MinIO console for the local media bucket, to eyeball what actually landed.
# Log in with STORAGE_ACCESS_KEY_ID / STORAGE_SECRET_ACCESS_KEY from .env.
http://localhost:9001
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
  **`User.username` is unique, and the refusal is `409 username_taken`** — one code for both
  writers (`POST /auth/register` and fastapi-users' own `PATCH /users/me`), because the client
  renders it under the same field either way. Enforced in `UserManager`, not in a route: the
  stock fastapi-users routers are where a username reaches the DB, so a route-level check
  would have had nowhere to live for the PATCH. Two layers on purpose — `is_username_taken`
  (`app/core/username.py`) is a probe, not a reservation, so a lost race still hits the unique
  index and `_conflict_as_api_error` maps *that* to the same 409 after rolling the session back
  (matched on the constraint name; any other IntegrityError stays a 500). Without the second
  layer the answer would be a bare 500 on the one field a user could have fixed themselves,
  which is exactly what it was.
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
  abandon — an exhausted channel drops the op instead of retrying it for 10 days. An *empty*
  channel is still parked (that backlog is how a new channel reaches its first subscriber).
  `FEED_RETRY_MAX_AGE_SECONDS` is the one knob for how long a post keeps looking for an
  audience (10 days), and it is deliberately generous: a parked op is one nobody had *room*
  for, so abandoning it early throws away reach the author paid for. A post that has
  genuinely run out of audience never waits it out — `has_eligible_recipient` abandons it on
  the spot. **`FEED_SEEN_TTL_SECONDS` is derived from it, not configured**
  (`FEED_SEEN_TTL_RETRY_MULTIPLE`, default 2 ⇒ 20 days): a seen set has to outlive every op
  still trying to deliver that post, no value for it makes sense independently of the retry
  deadline, and getting the pair wrong fails *silently* — the set expires mid-retry, the next
  fan-out forgets who has had the post, and someone is handed one they already reviewed. It
  is a `@property`, so setting `FEED_SEEN_TTL_SECONDS` in an env now fails at startup
  (`extra="forbid"`) rather than being quietly ignored. Why a *multiple* rather than a small
  margin: the set's clock runs from the post's last delivery while an op's deadline runs from
  its park, so the set must cover the retry window *plus* however long the post sat
  undelivered before that op existed. Dwell time has no hard bound, which is why the unique
  `(user, post)` constraint stays the real backstop rather than this.
- **Keeping the feed current** (`GET /posts/feed`, `GET /posts/feed/status`): the review queue
  is something the worker *pushes into*, so a client that only fetches once reads a list that
  can only ever shrink — the reason the app used to need a reload button. `/posts/feed/status`
  is the cheap counterpart the client polls while the feed is on screen: one `LRANGE`, no
  Postgres, no presigning, answering with the queue's **post ids** rather than a count. Ids,
  because a client that remembers which ones it has already pulled can then distinguish a real
  arrival from "the same posts, minus the ones I reviewed" exactly — with a count, a channel
  filter alone would make every single tick look like news and provoke a full feed fetch
  forever. It marks the user active for the same reason the feed read does: someone watching is
  a reader. `capacity` is `FEED_QUEUE_MAX_SLOTS`, and a full queue is worth naming separately —
  nothing more can be *placed* until the reader reviews something, which is the opposite
  instruction from "nothing has been published for you yet".
  Two things follow for `/posts/feed`. Its `limit` now defaults to `FEED_QUEUE_MAX_SLOTS`
  instead of 20, so a client that omits it always holds the whole queue — a page size fixed
  client-side would silently stop topping up the day that cap is raised past it. And
  `channel_id` is filtered in SQL *before* `skip`/`limit` rather than after the slice, which it
  used to be: filtering a page returned fewer than `limit` posts and left the rest of that
  channel unreachable at any offset. Reading the whole queue to do it costs nothing worth
  saving — it is capped by construction.
  What this deliberately does **not** add is a pull-based top-up. There is real un-delivered
  supply (posts published before you subscribed, whose fan-out went elsewhere), and
  `backfill_queue` would hand it over — but reach is what an author *paid* for, `FEED_FANOUT`
  recipients per operation, so serving those posts on demand would be free reach and an
  economy change, not a UX one. A feed that runs dry with nothing queued is the economy
  working; see the todo.
- **A post's score is disclosed once, after the verdict** (`POST /posts/{id}/review`):
  `post_forwarded_count` and `post_reviewed_count` (everyone who forwarded *or* dropped
  it — the denominator, so a client can say "2 of 9" rather than a bare count), both
  including the review that just happened. `Post.forwarded_count`/`dropped_count` are
  **deliberately absent from `PostRead`**, so no read route carries them: a reader who
  can see that everyone else forwarded a post is voting on the crowd rather than on the
  post, and hiding it client-side would leave the raw API as the way around that. That is
  the whole rule — the counters themselves are old, already maintained by `review_post`
  and already summed into `GET /stats`, so this costs no column, no index and no extra
  query. The two knock-on details: the post's counters are incremented **SQL-side**
  (`post.forwarded_count = Post.forwarded_count + 1`) rather than in Python, because a
  post is fanned out to `FEED_FANOUT` readers at once and its row is the one here that
  concurrent requests actually contend for; and that leaves the attributes expired, which
  an async session cannot resolve lazily, hence the explicit `session.refresh(post, [...])`
  after the commit. Note what is *not* claimed: this is reviewers, not viewers — someone
  holding the post in their queue is uncounted, and there is no delivery counter.
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
- **Outbound email** (`backend/app/core/email.py`): `send_email(to, subject, body)` is the only
  entry point the codebase knows, and `EMAIL_PROVIDER` picks which connector it hands the
  message to — `"smtp"` (default, `aiosmtplib`, any relay) or `"lettermint"`
  (https://lettermint.co, an EU transactional provider, through its official `lettermint` SDK).
  Adding the second one changed nothing about the first: the default is still SMTP, so an
  environment that sets nothing behaves exactly as it did. Three things are load-bearing:
  - **Only the SMTP connector logs instead of sending.** An unset `SMTP_HOST` is the *default*
    state, so that fallback can only fire where nobody asked for real mail — which is what makes
    it safe to log the address and the body there, and why a dev reads the verification code out
    of `docker compose logs`. Naming a connector is the opposite: an explicit act whose only
    purpose is delivery. So `EMAIL_PROVIDER=lettermint` without `LETTERMINT_API_TOKEN` **fails at
    startup** (`Settings.require_token_for_lettermint`) rather than degrading — degrading would
    print verification codes into a production log stream, which is the one thing the logging
    rules forbid, and it would fail *silently* until a user reported that no code arrived.
    That check is a **field** validator, not a model one, for the same reason it exists:
    pydantic quotes the validated input in the error it raises, and a model validator's input is
    the whole settings dict — so the crash meant to protect a secret would print `SECRET_KEY`
    into the log on its way out. `email.delivery_configured()` reports the same fact on the
    `app.started` line.
  - **A Lettermint client per send, never a shared one.** The SDK's email builder is mutable and
    *cached on the client* (`client.email` returns the same object every time), so two coroutines
    sharing one client would interleave their `.to()`/`.subject()` calls and mail one user
    another user's verification code. A fresh client per message costs a TLS handshake at a
    volume of roughly one mail per registration, and makes the hazard unexpressible rather than a
    comment someone has to remember.
  - **`LETTERMINT_FROM_EMAIL`/`_NAME` fall back to `SMTP_FROM_*`**, so there is nothing extra to
    set in the normal case. They exist at all because a from-address is provider-scoped —
    Lettermint only accepts a domain verified inside *its* account — so one shared setting could
    not express both the day those differ.
  Two smaller things the first live send taught. The SDK's exceptions **stringify to
  nothing usable** — a rejected send raises `ValidationError: Validation error:
  ValidationError` and the caller's `log.exception` then writes a traceback naming no
  cause; the API's actual field errors are on `.response_body`, so the connector logs
  them itself (`email.lettermint_rejected`) with the recipient scrubbed back out, since
  Lettermint quotes values in some of its messages. And `tests/conftest.py`'s
  `no_outbound_email` pins the connector per test: `EMAIL_PROVIDER` has no `TEST_`
  counterpart the way `DATABASE_URL` does, so without it a developer with
  `EMAIL_PROVIDER=lettermint` in `.env` makes every registration test in the suite fire
  a real API call at a real provider, and the SMTP tests quietly stop testing SMTP.
  Note the `httpx` pin in `pyproject.toml` is now a range with a real ceiling: lettermint needs
  `>=0.27`, and `0.28` removed the `AsyncClient(app=...)` shortcut `tests/conftest.py` builds the
  test client with.
- **Email bodies** (`backend/app/core/email_templates.py`): the HTML and matching
  plain-text bodies, kept apart from `email.py` (which is about *connectors*) and
  `email_verification.py` (about codes and Redis). `send_email(to, subject, body, html=None)`
  sends `multipart/alternative` when `html` is given — **`body` is never optional**, since a
  client set to prefer text would otherwise show nothing, and an HTML-only alternative is a
  spam signal by itself. Three things are load-bearing:
  - **It is markup from 2005 on purpose.** Nested tables and inline `style` attributes,
    because Gmail strips `<style>` in some contexts and Outlook renders through Word's
    engine, so neither flexbox nor grid can be relied on. No external assets either: remote
    images are blocked by default in most clients, so a logo image renders as a broken box
    for a first-time recipient — and the code itself must stay selectable text so it can be
    copied and read aloud. `tests/core/test_email_templates.py` asserts each of these,
    because every one of them fails *silently* in exactly one popular client.
  - **Copy is localized here rather than via `api_error` codes.** That contract works by
    handing a code to the client and letting its `.arb` supply the words; there is no client
    in an inbox, so the words have to live on this side. Same situation as `banner.py`, same
    shape: one dict per locale. `_t` falls back **per key**, so a half-finished translation
    degrades to one English sentence rather than raising and failing a send that carries a
    credential someone is waiting on.
  - **Locale reaches the mail two different ways**, because the three senders differ: the
    resend route takes `CurrentLocale`, while fastapi-users' `on_after_register` /
    `on_after_update` hooks get no dependency injection — only a `Request` — so
    `_locale_of()` in `app/deps/users.py` does the same resolution one level lower, and falls
    back to `DEFAULT_LOCALE` when a hook fires outside a request at all.
  Colors track the Flutter app's palette (`lib/src/core/theme/app_colors.dart`) — emerald
  accent, neutral greys — so the mail and the app read as one product.
- **Object storage** (`app/core/storage.py`, `app/core/sigv4.py`): every uploaded image, video and
  poster frame lives in an S3-compatible bucket — a **MinIO container** in docker-compose locally
  and in CI, a **Railway Bucket** (Tigris) in production. Nothing in the code knows which; the
  `STORAGE_*` settings are the whole difference (see RAILWAY.md, `env-template`). Four things are
  load-bearing:
  - **Clients never hold a bucket credential.** They are handed a **presigned URL**, minted only
    after the existing authorization check has passed. So the check moved from *every byte fetch* to
    *once per serialization*: a URL keeps working for up to `MEDIA_URL_TTL_SECONDS` even if access
    is revoked, and it is shareable by whoever holds it. That is the price of not proxying bytes;
    keep the TTL modest.
  - **Object keys carry no identity.** Post-media keys are flat random UUIDs, deliberately *not*
    derived from the author — an anonymous post's media URL is shown to every recipient, so an
    author-derived key would undo exactly what the EXIF strip protects.
  - **Presigning is deterministic within a window.** `presigned_url` quantizes its signing timestamp
    to a `MEDIA_URL_REFRESH_SECONDS` boundary, so the same object yields a byte-identical string
    until the window rolls over. The Flutter client caches media keyed by URL; a per-request
    signature would silently re-download the whole feed on every refresh. This is also the reason
    SigV4 is implemented here rather than via boto3 — botocore stamps the signing time from the wall
    clock and gives no way to pin it.
  - **The bucket endpoint must be reachable by the client**, even though the bucket is private. The
    host is a *signed* header, so a URL signed for `minio:9000` cannot be rewritten to
    `localhost:9000` afterwards — hence `STORAGE_PUBLIC_ENDPOINT_URL` alongside
    `STORAGE_ENDPOINT_URL`, and the published port in `docker-compose.override.yml`. A Flutter
    **web** client additionally needs CORS on the bucket (`MINIO_API_CORS_ALLOW_ORIGIN` locally).
- **Profile pictures** (`app/api/users.py`): stored in the bucket, keyed by `User.profile_picture_key`.
  Set via `PUT /users/me/profile-picture` (multipart), cleared via `DELETE`. The bytes go through
  `media_validation.process_profile_picture` like every other upload — decoded, EXIF-stripped,
  downscaled to `PROFILE_PICTURE_MAX_DIMENSION_PX` and re-encoded, so the stored `Content-Type` is
  derived rather than believed. That was the one upload path that trusted the client, which was
  survivable while the bytes sat in Postgres behind an authenticated route and is not now that the
  object is served straight from the bucket to whoever holds the presigned URL. There is **no GET route** — what
  `UserRead`/`PostAuthor` expose is `profile_picture_url`, a presigned link straight to the bucket,
  so the bytes never pass through this process. The anonymity rule needs no new code:
  `_serialize_post` populates the field only inside its existing `reveal_author` branch, so an
  anonymous post withholds the picture along with the id and username. Unlike the old in-DB version,
  **every upload writes a new key**, so a replaced picture produces a new URL and invalidates client
  caches by itself; the superseded object is deleted after the row commits.
- **Post media** (`app/core/media_validation.py`, `app/models/post_media.py`): images and
  videos attached to a post, stored in the bucket (`PostMedia.object_key` /
  `.poster_object_key`) — the row keeps only the metadata a feed needs to lay the block out.
  `process_upload` is the choke point — nothing reaches storage or spends a token before it.
  Uploads happen *before* the transaction commits, so a storage failure aborts the post; the
  residual failure mode is an orphaned object on rollback, which is the cheap one. Three
  things are load-bearing:
  - **Two fixed aspect ratios**, `POST_MEDIA_LANDSCAPE_RATIO` (4:3) and
    `POST_MEDIA_PORTRAIT_RATIO` (4:5), and the two media kinds reach them by opposite
    routes. An **image** is cropped in the Flutter client — the only place that can show
    an author what the crop discards — and merely *validated* here, so a wrong shape is
    `400 post_media_invalid_aspect_ratio`, never a silent server-side crop. A **video**
    cannot be re-encoded in a Flutter client at all, so the client sends only a
    `PostBlockIn.orientation` and the center crop happens in the transcode that was
    already running (falling back to `nearest_orientation` when omitted). Consequence:
    `PostMedia.width/height` stay nullable and a client must treat missing dimensions as
    "unknown, letterbox it" — but rows predating the columns are no longer *left* that
    way: `python backfill_post_media.py` re-runs the whole pipeline (transcode, crop,
    measure, poster) over clips already in Postgres, which is the fix for an old video
    rendering as a black rectangle.
  - **Every video carries a poster frame** (`PostMedia.poster_object_key`), its own object so
    a feed card can show a preview without ever touching the clip. Behind the same view gate
    as the clip, since a poster is a frame *of* it — enforced one step earlier now: a viewer
    who cannot open the post is never handed either URL. Taken from the transcoded output, so
    it is cropped and scaled identically, and from a moment slightly in rather than frame 0,
    which is routinely a black fade-in. Extraction is deliberately **non-fatal** — a clip
    that transcodes but yields no frame is still a good clip, so `poster_url` is nullable
    and the client falls back to a neutral tile.
  - **The byte-serving routes are gone**, and with them `app/core/http_range.py`. `Range`
    requests — the only way `video_player`'s native ExoPlayer/AVPlayer can scrub — are
    answered by the bucket, which actually streams, instead of by slicing a fully-loaded
    in-memory buffer. The deferred-column/`populate_existing` machinery that kept a feed
    query from dragging whole videos through the ORM went with them: nothing large is in
    Postgres to drag.
  EXIF is *applied* (`ImageOps.exif_transpose`) before it is stripped: a phone "portrait"
  photo is often a landscape sensor frame plus a rotate-90 tag, which would otherwise be
  stored sideways and measured against the wrong ratio.
- **Feedback** (`backend/app/api/feedback.py`, `app/models/feedback.py`): in-app feedback, bug
  reports and feature requests, with optional screenshots/screen recordings. Four things are
  load-bearing:
  - **It works signed out.** The form is linked from the login/register screens as well as the
    profile, because "I can't sign in" is unreportable from inside the app — so this is the one
    route using `OptionalUser` (`app/deps/users.py`). The route does *not* trust the form: with no
    user, `is_anonymous` is forced on and `allow_contact` off, so a crafted request cannot attach
    itself to an account, and a signed-in request whose token expired degrades to an anonymous
    submission rather than a rejection. `limit_feedback` is likewise the one rate limiter that
    keys on **client IP** (`request.client.host`, never the spoofable `X-Forwarded-For`) when
    there is no user id — behind a proxy that throttles more than intended, which is the safe
    direction.
  - **Anonymity is the absence of the link.** An anonymous submission stores **no `user_id` at
    all**, rather than storing one behind a flag — unlike `Post.author_id`, which anonymous posts
    still carry because fan-out needs it. `is_anonymous` is kept only so a NULL id reads as a
    choice rather than as missing data.
  - **No contact address is stored.** `allow_contact` is consent to be reached *as an account*;
    `Feedback.contact_email` is a **property** resolving off the linked `user` at serialization
    time (hence `lazy="selectin"` on that relationship — a lazy load in a property read during
    serialization is a hard error under async). Deliberately not a snapshotted column: `user_id`
    is `ON DELETE SET NULL`, so erasing an account already cuts the link, whereas a copied address
    would outlive the erasure and leave stray PII in this table. It also can't go stale. The
    report itself survives the erasure — only the ability to answer it goes.
  - **`locale` stays a column and is not derivable from the account.** There is deliberately no
    `User.locale` in this codebase (see Localization below), and the rows that most need it are
    the anonymous ones, which have no user to read anything from.
  - **`status` (`FEEDBACK_STATUSES`: `new`/`viewed`/`noted`/`done`, `new` on arrival) is internal
    triage state with no logic behind it yet** — nothing reads or writes it, there is no
    transition rule and no route sets it; it is exposed read-only on the superuser listing. A
    submitter cannot set it, and not because it is filtered: `feedback_create_form` is an explicit
    allow-list of form fields and the route builds the row itself, so `status`,
    `user_agreed_data_saving_at`, `user_id`, `locale` and `created` are all simply unreachable
    from a request. Kept a plain string, not a DB enum, so adding a state later is a value rather
    than a migration; no index until something actually filters on it.
  - **`user_agreed_data_saving_at` is what makes the row lawful, and it is server-stamped.**
    Without `consent=true` nothing is written (`feedback_consent_required`) and the column is NOT
    NULL, so a row existing *is* the evidence that consent preceded it; the client never sends a
    time it could backdate.
  - **The two fixed post ratios must not apply here**, which is the whole reason
    `process_feedback_upload` exists beside `process_upload` rather than calling it: a screenshot
    is whatever shape the reporter's screen is, and a screen recording center-cropped to 4:5 loses
    the bug. Images are stripped and downscaled without a ratio check (and a PNG stays PNG — JPEG
    ringing lands on exactly the text being reported); video runs the same transcode with
    `crop_to_ratio=False`. EXIF is still stripped, because a submission can be anonymous.
  Read back via superuser-only `GET /feedback` (there is no per-user view — a submission may
  carry no user to scope one to).
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
- **Logging** (`backend/app/core/logger.py`, `app/core/request_logging.py`): one stdout handler,
  configured from `create_app()` (which is *after* uvicorn installs its own config, hence the
  explicit reclaiming of the `uvicorn.*` loggers). `LOG_FORMAT=auto` emits **console** lines
  locally and **JSON** on Railway, where each top-level key of a JSON line becomes a filterable
  attribute — the reason structured fields are flattened into the payload rather than nested.
  Five things are load-bearing:
  - **The message is an event name, the values are keywords**: `log.info("post.created",
    post_id=..., price=...)`, never prose with values interpolated in. An event name is
    countable — an error rate or a "posts per hour" panel is a filter on one field rather than a
    regex over English. `get_logger(__name__)` returns the adapter that accepts those keywords;
    it redefines the level methods rather than inheriting them so that a field named `level` or
    `msg` cannot collide with a parameter and raise at the call site.
  - **The request id is the join.** `RequestLoggingMiddleware` opens a contextvar holding
    request id, method, path and client IP; the auth dependencies in `app/deps/users.py` bind
    `user_id` onto it as they resolve, and a `logging.Filter` stamps the lot onto every record
    emitted anywhere underneath. The id goes back to the client in `X-Request-ID` **and in the
    body of a 500** (`detail.request_id`), so a user-reported failure is one query
    (`@request_id:"..."`) away from the traceback. The context is a *mutable dict* on purpose:
    `user_id` is bound long after the middleware opened it and still has to reach the
    access-log line the middleware writes after the response. The worker opens the same kind of
    context per operation (`op-…`), so fan-out is traceable without an HTTP request.
  - **Errors are logged once, in the middleware.** An unhandled exception passes through that
    frame — the only one with the context bound — before Starlette's `ServerErrorMiddleware`
    turns it into a response, so the traceback is written there (`http.request_failed`) and
    `_unhandled_exception_handler` in `factory.py` deliberately writes none. That handler also
    reads the id off `scope` rather than the contextvar, because it runs *outside* the
    middleware, after the context has been reset. (uvicorn's own "Exception in ASGI application"
    line is a third-party duplicate of the traceback with no request id; ours is the one to
    read.)
  - **Volume is a design constraint, not an afterthought.** INFO is one line per request plus one
    per *user action*, so it scales with traffic and not with work. Anything per-retry, per-item
    or per-poll is DEBUG — `feed.op_parked` most of all, since a parked op re-parks every
    `FEED_RETRY_INTERVAL_SECONDS` for up to ten days and would emit ~43k lines on its own at
    INFO. `LOG_QUIET_PATHS` drops the two timer-driven endpoints (`/health`,
    `/posts/feed/status`) to DEBUG for the same reason. `LOG_SLOW_REQUEST_MS` promotes a slow
    request to WARNING whatever its status, and `LOG_LEVEL_OVERRIDES` (e.g. `{"app.feed":
    "DEBUG"}`) turns one module up on a live deploy without turning the process up.
  - **Log ids, never contents.** No email addresses (`user_id` is one join from one, for whoever
    is entitled to look), no post or feedback text, no tokens, no query strings — the one
    deliberate exception being `send_email`'s unconfigured-SMTP branch, which *is* the delivery
    mechanism in dev and cannot fire where a relay exists. `LOG_CLIENT_IP` is a flag because an
    IP is personal data under GDPR; it defaults on because signed-out abuse is otherwise
    uninvestigable. *Where* that address is read from is derived rather than configured
    (`_client_ip`): the socket peer normally, the **rightmost** `X-Forwarded-For` entry on
    Railway, where the peer is always the edge proxy. Rightmost because a proxy appends, so
    the last entry is the one our own hop wrote and the first is the caller's to invent — and
    a setting for this would be one whose wrong value degrades in silence. See RAILWAY.md for the operational side (queries worth saving, and what
    Railway does and does not offer for monitoring — `SENTRY_DSN` is still an unwired setting).
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
