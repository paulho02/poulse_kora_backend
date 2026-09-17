import asyncio
import os
import socket
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from fastapi_users import FastAPIUsers
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse

from app.api import api_router
from app.core import email
from app.core.config import settings
from app.core.errors import detail_text, slugify_detail
from app.core.logger import configure_logging, get_logger, resolved_log_format
from app.core.body_limit import BodySizeLimitMiddleware
from app.core.request_logging import RequestLoggingMiddleware
from app.core.storage import storage
from app.deps.rate_limit import limit_login, limit_register
from app.deps.users import fastapi_users, jwt_authentication
from app.feed import service
from app.feed.worker import run_consumer
from app.redis import redis_client
from app.schemas.user import UserCreate, UserRead, UserUpdate

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Run the feed operation-stream consumer and the price-snapshot refresher as
    in-process background tasks.

    Each process joins the consumer group under its own unique name, so running several
    web processes/replicas simply adds consumers that share the load — the Streams
    group delivers each op to exactly one, and XAUTOCLAIM reclaims any left by a crash
    (see app/feed/worker.py). For heavy fan-out, prefer a dedicated worker deployment
    over piling consumers onto web processes, but correctness no longer depends on it.

    The price refresher (app/feed/service.py: run_price_refresher) has no per-consumer
    identity to join — every process just recomputes and overwrites the same shared
    snapshot key on its own timer, which is harmless since the computation is
    deterministic given the same Redis state.

    Also creates the media bucket when S3_AUTO_CREATE_BUCKET is on (local dev
    and CI only — on Railway the platform provisions it), and closes the storage
    client's connection pool on the way out.
    """
    await storage.ensure_bucket()
    consumer_name = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    task = asyncio.create_task(run_consumer(redis_client, consumer_name))
    price_task = asyncio.create_task(service.run_price_refresher(redis_client))
    app.state.feed_consumer_task = task
    app.state.price_refresher_task = price_task
    started_at = time.time()
    # The first line of a deploy, and the one to read when a deploy behaves
    # unlike the last one: it names the flags that decide behaviour, so "why is
    # nobody getting verification emails" is answerable from the log alone.
    log.info(
        "app.started",
        consumer=consumer_name,
        log_level=settings.LOG_LEVEL,
        log_format=resolved_log_format(),
        require_email_verification=settings.REQUIRE_EMAIL_VERIFICATION,
        email_provider=settings.EMAIL_PROVIDER,
        email_delivery_configured=email.delivery_configured(),
        google_oauth_enabled=settings.GOOGLE_OAUTH_ENABLED,
        subscriptions_enabled=settings.SUBSCRIPTIONS_ENABLED,
        storage_bucket=settings.S3_BUCKET_NAME,
        feed_fanout=settings.FEED_FANOUT,
        queue_slots=settings.FEED_QUEUE_MAX_SLOTS,
    )
    try:
        yield
    finally:
        log.info("app.stopping", uptime_seconds=round(time.time() - started_at, 1))
        task.cancel()
        price_task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        try:
            await price_task
        except asyncio.CancelledError:
            pass
        await storage.aclose()


def create_app():
    # Before anything else: uvicorn has already installed its own logging config
    # by the time it imports this module, so this is where it is taken back (see
    # app/core/logger.py: configure_logging).
    configure_logging()
    description = f"{settings.PROJECT_NAME} API"
    app = FastAPI(
        title=settings.PROJECT_NAME,
        openapi_url=f"{settings.API_PATH}/openapi.json",
        docs_url="/docs/",
        description=description,
        redoc_url=None,
        lifespan=lifespan,
    )
    setup_routers(app, fastapi_users)
    setup_exception_handlers(app)
    setup_cors_middleware(app)
    serve_static_app(app)
    # Inside the logging middleware (added before it, so wrapped by it): a body
    # refused at the door is still one request line, with its 413, in the log.
    app.add_middleware(BodySizeLimitMiddleware)
    # Added last, so it ends up outermost (Starlette runs user middleware in
    # reverse registration order): the status it logs is the one the client
    # actually received, after CORS and after the SPA fallback have had their
    # say, and the request context it opens is in place for every other
    # middleware as well as the routes.
    app.add_middleware(RequestLoggingMiddleware)
    return app


def setup_exception_handlers(app: FastAPI) -> None:
    """Force every error response into the `{"detail": {"error": ..., ...}}` envelope.

    Our own routes already raise that shape via `app.core.errors.api_error`, but
    FastAPI, Starlette and fastapi-users all raise their own errors with either a
    bare string detail (`"Not Found"`) or a differently-keyed dict. The client has
    to be able to switch on one field, so normalize them all here rather than
    teaching the client three formats.
    """

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
        # A 5xx raised deliberately (media_storage_unavailable, email_send_failed)
        # is still a broken dependency, and the route that raised it has usually
        # logged the cause already - this records that the client was told, with
        # the code it was told. 4xx are the client's business and are covered by
        # the one access-log line.
        if exc.status_code >= 500:
            log.error("http.server_error_response", status=exc.status_code)
        detail = exc.detail
        if isinstance(detail, dict):
            # Already structured. fastapi-users uses `code`/`reason` for password
            # validation failures, so promote that to `error` rather than leaving
            # the client with an envelope it can't key on.
            body = dict(detail)
            if "error" not in body:
                body["error"] = slugify_detail(detail_text(body.pop("code", "error")))
        else:
            text = detail_text(detail)
            body = {"error": slugify_detail(text), "message": text}
        return JSONResponse(
            {"detail": body}, status_code=exc.status_code, headers=exc.headers
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(
        request: Request, exc: RequestValidationError
    ):
        return JSONResponse(
            {
                "detail": {
                    "error": "validation_error",
                    "fields": [
                        {
                            "field": ".".join(str(p) for p in err["loc"][1:]),
                            "message": err["msg"],
                        }
                        for err in exc.errors()
                    ],
                }
            },
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception):
        # Deliberately opaque about the cause: the client shows a generic
        # "something went wrong". It is not opaque about *which* failure it was —
        # `request_id` is the one field that turns a user saying "it broke" into
        # the exact traceback, via `@request_id:"..."` in the log explorer.
        #
        # Read off the scope rather than the log context: this handler is invoked
        # by Starlette's ServerErrorMiddleware, which sits *outside*
        # RequestLoggingMiddleware, so by now the context has been reset. The
        # traceback is not logged here either, for the same reason — the frame
        # that still had the context logged it (`http.request_failed`), and a
        # second copy here would only double the error count.
        body: dict[str, str] = {"error": "internal_error"}
        request_id = request.scope.get("request_id")
        if not request_id:
            return JSONResponse({"detail": body}, status_code=500)
        body["request_id"] = request_id
        # Set here as well as in the middleware: this response is produced
        # *outside* it (ServerErrorMiddleware holds the outer `send`), so the
        # header the middleware adds to every other response would be missing
        # from exactly the ones that most need it.
        return JSONResponse(
            {"detail": body}, status_code=500, headers={"X-Request-ID": request_id}
        )


def setup_routers(app: FastAPI, fastapi_users: FastAPIUsers) -> None:
    app.include_router(api_router, prefix=settings.API_PATH)
    # The two signed-out writers fastapi-users serves get their budgets here,
    # as router-level dependencies, because there is no route of ours to attach
    # them to. `limit_login` also covers `/logout` on the same router, which is
    # harmless: it is a no-op for a JWT strategy and nobody calls it in a loop.
    app.include_router(
        fastapi_users.get_auth_router(
            jwt_authentication,
            requires_verification=False,
        ),
        prefix=f"{settings.API_PATH}/auth/jwt",
        tags=["auth"],
        dependencies=[Depends(limit_login)],
    )
    app.include_router(
        fastapi_users.get_register_router(UserRead, UserCreate),
        prefix=f"{settings.API_PATH}/auth",
        tags=["auth"],
        dependencies=[Depends(limit_register)],
    )
    app.include_router(
        fastapi_users.get_users_router(
            UserRead, UserUpdate, requires_verification=False
        ),
        prefix=f"{settings.API_PATH}/users",
        tags=["users"],
    )
    # The following operation needs to be at the end of this function
    use_route_names_as_operation_ids(app)


def serve_static_app(app):
    """Serves the (currently frontend-disabled, vestigial) static bundle as a
    SPA fallback for unmatched GET/HEAD requests only.

    Deliberately *not* `app.mount("/", ...)`, and not even a registered route
    at all — just a plain post-routing middleware check. A mount (or any
    route) registered at "/" matches literally any path, method included,
    since neither `Mount` nor a method-restricted `Route` can be excluded from
    matching a path outright — only from matching a *method* on that path.
    That's enough to turn any non-GET/HEAD request to an unmatched path (a
    typo'd path, a missing `/api/v1` prefix, or a CORS preflight OPTIONS
    request when CORS isn't configured to intercept it first) into a
    synthesized 405 "path exists, wrong method" instead of a normal "path
    doesn't exist" 404 — and, worse, masks a genuine wrong-method call on a
    real endpoint the same way. Only reaching for a static/index.html
    response after the real router has already produced its own 404 keeps
    Starlette's own 404/405 semantics authoritative everywhere else.
    """
    static_files = StaticFiles(directory="static")

    @app.middleware("http")
    async def _static_fallback_middleware(request: Request, call_next):
        response = await call_next(request)
        if request.method not in ("GET", "HEAD") or response.status_code != 404:
            return response
        path = request["path"]
        if path.startswith(settings.API_PATH) or path.startswith("/docs"):
            return response
        try:
            return await static_files.get_response(path.lstrip("/"), request.scope)
        except StarletteHTTPException:
            return FileResponse("static/index.html")


def setup_cors_middleware(app):
    if settings.BACKEND_CORS_ORIGINS:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[
                str(origin).rstrip("/") for origin in settings.BACKEND_CORS_ORIGINS
            ],
            allow_credentials=True,
            allow_methods=["*"],
            expose_headers=["Content-Range", "Range"],
            allow_headers=["Authorization", "Range", "Content-Range"],
        )


def use_route_names_as_operation_ids(app: FastAPI) -> None:
    """
    Simplify operation IDs so that generated API clients have simpler function
    names.

    Should be called only after all routes have been added.
    """
    route_names = set()
    for route in app.routes:
        if isinstance(route, APIRoute):
            if route.name in route_names:
                raise Exception("Route function names should be unique")
            route.operation_id = route.name
            route_names.add(route.name)
