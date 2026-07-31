# Deploying to Railway

Postgres and Redis are assumed already provisioned as Railway services in the project. This
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
| `REQUIRE_EMAIL_VERIFICATION` | already `true` by default | Keep it, but it's a no-op (codes only get logged, never delivered) until SMTP is configured — see below. |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USERNAME` / `SMTP_PASSWORD` / `SMTP_FROM_EMAIL` | required if `REQUIRE_EMAIL_VERIFICATION=true` | Any relay works (Gmail SMTP, SES, Mailgun, Postmark, ...). |
| `SENTRY_DSN` | optional | Recommended once real users are on it. |
| `TEST_DATABASE_URL` / `TEST_REDIS_URL` | not needed | Dev/CI-only, used only when `pytest` is running. |

`app/core/config.py` already normalizes a `postgres://`-scheme `DATABASE_URL` to `postgresql://`,
so whatever scheme Railway's Postgres reference variable uses works unchanged.

## 3. First deploy checklist

- Migrations run automatically on every boot (`entrypoint.sh` → `alembic upgrade head`) — nothing
  manual needed here, including for schema changes on future deploys.
- **`rebuild_redis.py` does *not* run automatically, and shouldn't.** Normal traffic (register,
  subscribe, post, review) already writes Redis state directly as it happens — there's no "initial
  state" that needs deriving from Postgres on a fresh deploy. Run it manually, once, only when:
  1. You turn on `FEED_EXCLUDE_SEEN` against a database that already has post/review history.
  2. Redis loses data (volume issue, manual flush) and needs reseeding from Postgres.
  3. You're importing pre-existing Postgres data into a Redis that never saw it.

  ```bash
  railway run --service <backend-service-name> python rebuild_redis.py
  ```
  It's idempotent — safe to re-run if unsure whether it already ran.
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
- **SMTP must be configured** for `REQUIRE_EMAIL_VERIFICATION` to do anything real — otherwise it's
  silently a no-op (see `app/core/email.py`: unset `SMTP_HOST` just logs and returns).
- **Rate limiting is already on by default** (`INTERACTION_RATE_LIMIT`/`INTERACTION_RATE_WINDOW_SECONDS`
  in `app/core/config.py`) — no action needed, just be aware it exists if load testing.
- **`/docs/` (OpenAPI UI) is publicly reachable by design** (`app/factory.py`) — acceptable for an
  int environment, worth revisiting (e.g. gate behind a superuser or disable) before a public launch
  with real user data.
- **`.env` stays out of git** (already gitignored) — never commit real credentials; use Railway
  Variables exclusively for deployed environments.

## Verifying a deploy

- `GET https://<backend-domain>/api/v1/health` → `{"msg": "ok"}` — this is also what Railway's own
  healthcheck polls before cutting traffic to a new deployment.
- `GET https://<backend-domain>/docs/` — OpenAPI UI, confirms static/app serving works.
- Tail the deploy logs for the `alembic upgrade head` output on boot to confirm migrations applied
  cleanly.
