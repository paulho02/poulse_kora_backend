# Environment gate

A Caddy service that puts a whole non-public Railway environment (dev, int) behind one password.
It is the only service with a public domain: it serves the Flutter web app and routes `/api/*` and
`/docs*` to the backend, reaching both over Railway's private network. Not used locally and never in
production.

## How it works

- The first page load answers with a Basic-auth prompt. On success the proxy sets an `HttpOnly`
  `peerkola_gate` cookie (30 days), and every later request is checked against the cookie, not
  against Basic credentials. The app's own `Authorization` header carries its JWT, so the gate cannot
  live in that header.
- `/api/*` without the cookie gets `401 {"detail": {"error": "gate_locked"}}` and no Basic challenge,
  so a background request never pops a browser dialog.
- Basic credentials the browser replays on its own are stripped before they reach the backend.
- The app and the API share one origin, so the cookie reaches API calls without CORS, and
  `BACKEND_CORS_ORIGINS` doesn't matter here.
- `X-Forwarded-For` is passed through unchanged. The backend takes the caller from the rightmost
  entry, which Railway's edge appends (`caller_address` in `app/core/request_logging.py`). If Caddy
  replaced the header with its own peer, every caller would share one rate-limit budget.
- The cookie value is derived from the user and password, so it survives restarts. Changing
  `GATE_PASSWORD` signs every browser out.
- `GET /healthz` is open, for Railway's healthcheck.

What it does *not* cover: presigned media URLs point straight at the bucket and bypass the gate.
They are signed and expire, the same as in production. Native app builds can't get past the gate,
because they have no cookie and no Basic prompt. Use the web app against a gated environment.

## Railway setup

1. **Backend and app services:** remove any public domain. Set `PORT` explicitly on each (for
   example `8000` and `8080`), so the private address has a fixed port to point at.
2. **Flutter web service:** set `API_BASE_URL` to the *proxy's* public URL (`https://<gate-domain>`)
   and redeploy. The bundle bakes it in at build time.
3. **New service** from this repo, Root Directory `proxy`, builder Dockerfile. Generate a public
   domain for it. Variables:

   | Variable | Value |
   |---|---|
   | `GATE_PASSWORD` | the shared password |
   | `GATE_USER` | optional, default `peerkola` |
   | `BACKEND_UPSTREAM` | `${{backend.RAILWAY_PRIVATE_DOMAIN}}:${{backend.PORT}}` |
   | `APP_UPSTREAM` | `${{app.RAILWAY_PRIVATE_DOMAIN}}:${{app.PORT}}` |

   Replace `backend`/`app` with your service names. Healthcheck path: `/healthz`.
4. **Bucket CORS** (web media): allow the gate's origin.

A `502` from the gate usually means the private address is wrong or not reachable. Uvicorn and nginx
listen on IPv4 only. That is fine on environments with IPv4 private networking (the default for new
ones), but older IPv6-only environments need them to listen on `::`.
