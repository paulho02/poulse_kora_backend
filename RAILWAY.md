# Deploying to Railway

Postgres, Redis and a **Bucket** are assumed already provisioned as Railway services in the
project (Bucket: New → Bucket; it is private by default, which is what this backend wants). This
covers the backend service. The Flutter web client lives in the sibling `poulse_kora_app` repo —
see that repo's own deploy notes for the web service.

## 1. Create the backend service

1. New Service → Deploy from GitHub repo → `poulse_kora_backend`.
2. Service Settings → **Root Directory**: `backend`. This restricts the build to that subtree, so
   `backend/Dockerfile`'s existing `COPY ./pyproject.toml ...` etc. keep working unchanged — it's
   the same context Docker Compose already builds with (`build: context: backend`).
3. Service Settings → **Config File Path**: `/backend/railway.json`. Config-as-code files are
   looked up relative to the true repo root regardless of Root Directory, so this has to be spelled
   out even though Root Directory is `backend`. `backend/railway.json` (already in the repo) sets
   the Dockerfile builder, the healthcheck (`GET /api/v1/health` — no DB/Redis touch, safe to probe
   before either is confirmed reachable), and a restart-on-failure policy.
4. Builder should now show "Dockerfile" (auto-detected). No custom start command needed —
   `backend/entrypoint.sh` (baked into the image) runs `alembic upgrade head` then starts uvicorn on
   `$PORT`, which Railway injects automatically.

## 2. Environment variables

Set these as Variables on the backend service (Settings → Variables):

| Variable | Required | Value |
|---|---|---|
| `DATABASE_URL` | yes | Reference var to your Postgres service, e.g. `${{Postgres.DATABASE_URL}}` |
| `REDIS_URL` | yes | Reference var to your Redis service, e.g. `${{Redis.REDIS_URL}}` |
| `SECRET_KEY` | yes | Generate per environment: `openssl rand -hex 32`. Never reuse a dev value. |
| `BACKEND_CORS_ORIGINS` | yes | JSON array of the exact `https://` origin(s) the Flutter web app is served from, e.g. `["https://<flutter-service>.up.railway.app"]`. See the bootstrapping note below — you won't have this value until step 4. |
| `REQUIRE_STRONG_PASSWORD` | recommended | `true` — defaults to `false`, which is fine for local dev only. Anything internet-reachable should turn this on. |
| `REQUIRE_EMAIL_VERIFICATION` | already `true` by default | Keep it, but it's a no-op (codes only get logged, never delivered) until an email connector is configured — see below. |
| `EMAIL_PROVIDER` | recommended | `smtp` (default) or `lettermint`. Picks which connector `send_email` uses; the other one's settings are then ignored. |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USERNAME` / `SMTP_PASSWORD` / `SMTP_FROM_EMAIL` | required if `EMAIL_PROVIDER=smtp` and `REQUIRE_EMAIL_VERIFICATION=true` | Any relay works (Gmail SMTP, SES, Mailgun, Postmark, ...). |
| `LETTERMINT_API_TOKEN` | required if `EMAIL_PROVIDER=lettermint` | A *sending* token from the Lettermint dashboard, not a team API token. The app refuses to boot without it — see below. Optional companions: `LETTERMINT_ROUTE`, `LETTERMINT_FROM_EMAIL`, `LETTERMINT_FROM_NAME`. |
| `AWS_ENDPOINT_URL` | yes | Set by the bucket's **Connect** action (`${{Bucket.ENDPOINT}}`, `https://storage.railway.app`) |
| `S3_BUCKET_NAME` | yes | Set by **Connect** (`${{Bucket.BUCKET}}`) |
| `AWS_DEFAULT_REGION` | yes | Set by **Connect** (`${{Bucket.REGION}}` — `auto`) |
| `AWS_ACCESS_KEY_ID` | yes | Set by **Connect** (`${{Bucket.ACCESS_KEY_ID}}`) |
| `AWS_SECRET_ACCESS_KEY` | yes | Set by **Connect** (`${{Bucket.SECRET_ACCESS_KEY}}`) |
| `S3_ADDRESSING_STYLE` | yes | `virtual` — Railway serves `https://<bucket>.storage.railway.app/<key>`. The `path` default exists only because `<bucket>.localhost` does not resolve against the local MinIO container. |
| `S3_PUBLIC_ENDPOINT_URL` | no | Leave unset. It exists for local dev, where the backend and the client reach MinIO under different hostnames; on Railway the endpoint is already the public one. |
| `S3_AUTO_CREATE_BUCKET` | no | Leave unset (`false`). The platform provisions the bucket and the credentials are scoped to it. |
| `MEDIA_URL_TTL_SECONDS` / `MEDIA_URL_REFRESH_SECONDS` | no | Defaults (1 h / 15 min) are fine. The first is how long a leaked media URL keeps working, the second how often the URL string changes — see the media section below before touching either. |
| `TEST_S3_BUCKET_NAME` / `TEST_S3_PUBLIC_ENDPOINT_URL` | not needed | Dev/CI-only, used only when `pytest` is running. |
| `SENTRY_DSN` | optional | Declared in `app/core/config.py` but **not wired to anything yet** — setting it today does nothing. See "Logs and monitoring" below. |
| `ENVIRONMENT` | recommended | `production` (or `int`). Stamped on every log line as `env`, which is what lets one log query tell two environments apart. |
| `LOG_LEVEL` | no | `INFO` default. `DEBUG` is for a few minutes of investigation, not a standing setting. |
| `LOG_FORMAT` | no | `auto` default → JSON on Railway, which is what makes the fields below filterable. Only set it to pin the format. |
| `LOG_LEVEL_OVERRIDES` | no | JSON object, e.g. `{"app.feed": "DEBUG"}` — turn one module up without turning the process up. |
| `LOG_QUIET_PATHS` / `LOG_SLOW_REQUEST_MS` / `LOG_ACCESS` / `LOG_SQL` / `LOG_CLIENT_IP` | no | Defaults are the intended production setting; see `app/core/logger.py` for what each trades. |

Those five are named the way they are on purpose: they are exactly the variables Railway's bucket
auto-connect injects into a service, so attaching a Railway Bucket sets all five with no manual
mapping — and any other S3 tool run inside the container (`aws s3`, boto3, rclone) picks the same
credentials up from the environment. The four settings that have no S3 convention to follow
(`S3_ADDRESSING_STYLE`, `S3_PUBLIC_ENDPOINT_URL`, `S3_AUTO_CREATE_BUCKET`, `TEST_S3_BUCKET_NAME`)
still have to be set by hand; `S3_ADDRESSING_STYLE=virtual` is the one that is *required* and that
auto-connect cannot know, since it describes how this backend addresses the bucket rather than how
it authenticates.

There is deliberately **no variable for reading the caller's IP out of `X-Forwarded-For`**. Behind
Railway's edge the socket peer is always the proxy, so the backend takes the address from that
header here and from the socket everywhere else — decided by the same Railway detection that picks
the JSON format, because which one is right is a fact about where the process runs rather than a
preference, and a flag set wrong would quietly log the proxy's address (or a forgeable one) with no
symptom. It reads the *rightmost* entry, the one Railway's own proxy appended; the leftmost is
whatever the caller chose to claim. This affects logs only — the rate limiter keys on the socket
address regardless (`app/deps/rate_limit.py`), since a forgeable identity there would be an opt-out
of the limit rather than a mislabelled line.
| `TEST_DATABASE_URL` / `TEST_REDIS_URL` | not needed | Dev/CI-only, used only when `pytest` is running. |

`app/core/config.py` already normalizes a `postgres://`-scheme `DATABASE_URL` to `postgresql://`,
so whatever scheme Railway's Postgres reference variable uses works unchanged.

## 3. First deploy checklist

- **Set the `AWS_*` / `S3_*` variables before the first deploy.** Media lives in the bucket and not in
  Postgres (`app/core/storage.py`), so an upload route raises without them — there is no
  degraded mode that stores bytes in the database. Nothing in the migrations needs them: the
  data migration that used to copy existing media out of Postgres was squashed away along with
  the byte columns it dropped, so `0001_initial_schema` creates the object-key columns directly
  and never touches the bucket.
- Migrations run automatically on every boot (`entrypoint.sh` → `alembic upgrade head`) — nothing
  manual needed here, including for schema changes on future deploys.
- **`scripts/dangerous/rebuild_redis.py` does *not* run automatically, and shouldn't.** Normal traffic (register,
  subscribe, post, review) already writes Redis state directly as it happens — there's no "initial
  state" that needs deriving from Postgres on a fresh deploy. Run it manually, once, only when:
  1. You turn on `FEED_EXCLUDE_SEEN` against a database that already has post/review history.
  2. Redis loses data (volume issue, manual flush) and needs reseeding from Postgres.
  3. You're importing pre-existing Postgres data into a Redis that never saw it.

  ```bash
  railway run --service <backend-service-name> python -m scripts.dangerous.rebuild_redis
  ```
  It's idempotent — safe to re-run if unsure whether it already ran. It is filed under
  `dangerous/` for one reason: it reseeds token balances from `FEED_STARTING_TOKENS +
  reviewed_count`, so a run **refunds every token spent since the last one**. That is fine for
  the three cases above and wrong as routine maintenance. Note the `-m` form — scripts are run
  as modules, not file paths (see `backend/scripts/README.md`).
- Single `uvicorn` process per replica (matches the existing Dockerfile/compose setup). The feed
  consumer and price-refresher background tasks in `app/factory.py`'s lifespan are already designed
  to be safe across multiple replicas (each joins the Redis Streams consumer group under a unique
  name). Scale horizontally later via `numReplicas` in `railway.json` if needed — no Dockerfile
  change required.

## 4. Deploy-order bootstrapping (backend ↔ Flutter web)

The Flutter web app bakes `API_BASE_URL` into its JS bundle at **build time** (`--dart-define`), so
there's a one-time chicken-and-egg step:

1. Deploy the backend first (steps above). Note its public Railway domain.
2. In the Flutter web service, set `API_BASE_URL` to that domain (`https://...`) and deploy it.
   Note *its* public domain.
3. Come back to the backend service and set `BACKEND_CORS_ORIGINS` to include the Flutter app's
   domain, then redeploy the backend once more.

After that, redeploying either service independently is fine — this ordering is only needed once,
or whenever the Flutter app's domain changes (e.g. adding a custom domain).

## 5. Security checklist for the "int" environment

- **HTTPS end-to-end.** Railway terminates TLS automatically on `*.up.railway.app` and on any
  custom domain you attach — don't add anything that would let the JWT bearer token travel over
  plain `http://`. Make sure `API_BASE_URL` (Flutter build var) and `BACKEND_CORS_ORIGINS` both use
  `https://`.
- **CORS is already structurally safe** — `BACKEND_CORS_ORIGINS` is a typed `AnyHttpUrl` list, so a
  wildcard `*` isn't even expressible, and it's combined with `allow_credentials=True` correctly
  (never combine a real wildcard with credentials). Just keep the origin list minimal and exact.
- **Turn on `REQUIRE_STRONG_PASSWORD`** (see table above) — it's off by default for local-dev
  convenience only.
- **`SECRET_KEY` must be a real random value**, not the `CHANGE_ME` placeholder from
  `env-template`/local `.env`.
- **An email connector must be configured** for `REQUIRE_EMAIL_VERIFICATION` to do anything real.
  On the default `EMAIL_PROVIDER=smtp`, an unset `SMTP_HOST` is silently a no-op (see
  `app/core/email.py`: it just logs the code and returns), which is the local-dev behaviour leaking
  into a deploy. `EMAIL_PROVIDER=lettermint` cannot fail this way — a missing `LETTERMINT_API_TOKEN`
  crashes the app at startup instead, deliberately, so that verification codes can never end up
  printed into a production log stream. Both cases are visible on the `app.started` line
  (`email_provider`, `email_delivery_configured`).
- **Sending domain**: whichever connector is live, the from-address has to be one that provider is
  allowed to send as. For Lettermint that means the domain is verified in the Lettermint account
  (DNS records on `poulse.com`); `LETTERMINT_FROM_EMAIL` exists for the case where it differs
  from the address the SMTP relay used.
- **Rate limiting is already on by default** (`INTERACTION_RATE_LIMIT`/`INTERACTION_RATE_WINDOW_SECONDS`
  in `app/core/config.py`) — no action needed, just be aware it exists if load testing.
- **`/docs/` (OpenAPI UI) is publicly reachable by design** (`app/factory.py`) — acceptable for an
  int environment, worth revisiting (e.g. gate behind a superuser or disable) before a public launch
  with real user data.
- **`.env` stays out of git** (already gitignored) — never commit real credentials; use Railway
  Variables exclusively for deployed environments.
- **Media URLs are capabilities, not just links.** The bucket is private and nothing in it is
  publicly readable, but `PostMediaRead.url` / `poster_url` / `profile_picture_url` are presigned:
  whoever holds one can fetch the object without logging in, until it expires. The authorization
  check runs when the URL is minted, not when it is used, so `MEDIA_URL_TTL_SECONDS` is the window
  in which a forwarded URL still works. One hour is the default; shorten it if that matters more
  than client-side caching, but see `MEDIA_URL_REFRESH_SECONDS` in `app/core/storage.py` first —
  the two are coupled, and a URL is only guaranteed `TTL - REFRESH` of life when handed out.
- **CORS on the bucket** matters only for the Flutter *web* client, which fetches presigned URLs
  cross-origin from the browser. Native builds are unaffected. If web images/videos fail with a
  CORS error while the same URL works in `curl`, that is the bucket's CORS configuration, not the
  signature.

## Logs and monitoring

Everything goes to stdout, which is all Railway needs. `LOG_FORMAT=auto` emits **JSON** whenever
`RAILWAY_ENVIRONMENT_NAME` is set, and Railway lifts each top-level key of a JSON log line into a
filterable attribute — that, plus the request id, is the whole "production errors should be
traceable" story. Full design notes are in `backend/app/core/logger.py`; the operational summary:

**Finding one user's problem.** Every response carries an `X-Request-ID` header, and every 500
body carries the same value as `detail.request_id` — so a user who can screenshot an error gives
you an exact key. In the Railway log explorer:

```
@request_id:"a1b2c3d4e5f6a7b8"
```

returns every line that request produced, in order, across every module — the access line, the
domain events, and the traceback. `@user_id:"<uuid>"` does the same for everything one account
did. A worker fan-out gets its own id of the form `op-…`, so a delivery problem is traceable the
same way even though no HTTP request is involved.

**Useful standing queries.**

| Question | Query |
|---|---|
| Is anything broken right now? | `@level:ERROR` |
| What is slow? | `@slow:true` (anything over `LOG_SLOW_REQUEST_MS`, default 1.5 s) |
| Are people being throttled? | `rate_limit.exceeded` |
| Is the economy behaving? | `feed.price_changed` — one line per actual price move, with the queue length and active-user count that caused it |
| Did the money flow? | `subscription.activated`, `subscription.renewed`, `payment.webhook_unknown_customer` |
| Is anyone signing up? | `user.registered`, `auth.login`, `auth.login_failed` |
| Are posts reaching people? | `post.created` vs `feed.op_abandoned` |
| Is the bucket healthy? | `storage.request_failed` |

**Volume.** One line per request plus one per user action, so it scales with traffic rather than
with work — a fan-out to three recipients is one line, not four. The two endpoints the client
polls on a timer (`/api/v1/health`, `/api/v1/posts/feed/status`) are logged at DEBUG, so they cost
nothing at INFO; at a few hundred users they would otherwise be the majority of the log. A rough
figure: at 500 daily active users doing ~40 actions each, expect low tens of thousands of INFO
lines a day. If that ever needs cutting further, `LOG_ACCESS=false` removes the per-request line
and leaves the domain events.

**Monitoring — what Railway does and does not give you.** The log explorer covers searching and
filtering, and its saved views plus the Observability dashboard can chart a query over time (an
`@level:ERROR` count widget is a serviceable error-rate panel). What Railway does *not* provide is
error *grouping* (5,000 occurrences of one bug shown as one issue with a trend), alerting on a log
query, or release-over-release regression tracking — its notifications are about deploy and crash
events, not about what the app logs. That is the gap `SENTRY_DSN` in the table above is meant for,
and it is currently an unwired setting: nothing reads it. Wiring it is small (add `sentry-sdk`,
call `sentry_sdk.init(dsn=..., environment=settings.ENVIRONMENT, release=RAILWAY_GIT_COMMIT_SHA)`
in `create_app`, and set the request id as a tag so a Sentry issue and a log query point at the
same request), but it adds a dependency and an external processor of user data, so it is left as a
deliberate decision rather than done by default.

## Verifying a deploy

- `GET https://<backend-domain>/api/v1/health` → `{"msg": "ok"}` — this is also what Railway's own
  healthcheck polls before cutting traffic to a new deployment.
- `GET https://<backend-domain>/docs/` — OpenAPI UI, confirms static/app serving works.
- Tail the deploy logs for the `alembic upgrade head` output on boot to confirm migrations applied
  cleanly.
- Look for the `app.started` line: it names the flags that actually took effect on that deploy
  (`email_provider`, `email_delivery_configured`, `require_email_verification`,
  `google_oauth_enabled`, `storage_bucket`,
  `log_format` — which should read `json` here). A misconfiguration is usually visible in that one
  line before any user finds it.
- Upload a profile picture from the app and confirm the returned `profile_picture_url` points at
  `https://<bucket>.storage.railway.app/...` and loads. A `SignatureDoesNotMatch` here almost always
  means `S3_ADDRESSING_STYLE` is still `path` — the host is part of the signature.
