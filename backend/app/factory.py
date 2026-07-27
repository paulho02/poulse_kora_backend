import asyncio
import logging
import os
import socket
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from fastapi_users import FastAPIUsers
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse

from app.api import api_router
from app.core.config import settings
from app.core.errors import slugify_detail
from app.deps.users import fastapi_users, jwt_authentication
from app.feed import service
from app.feed.worker import run_consumer
from app.redis import redis_client
from app.schemas.user import UserCreate, UserRead, UserUpdate

logger = logging.getLogger(__name__)


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
    """
    consumer_name = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    task = asyncio.create_task(run_consumer(redis_client, consumer_name))
    price_task = asyncio.create_task(service.run_price_refresher(redis_client))
    app.state.feed_consumer_task = task
    app.state.price_refresher_task = price_task
    try:
        yield
    finally:
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


def create_app():
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
        detail = exc.detail
        if isinstance(detail, dict):
            # Already structured. fastapi-users uses `code`/`reason` for password
            # validation failures, so promote that to `error` rather than leaving
            # the client with an envelope it can't key on.
            body = dict(detail)
            if "error" not in body:
                body["error"] = slugify_detail(str(body.pop("code", "error")))
        else:
            body = {"error": slugify_detail(str(detail)), "message": str(detail)}
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
        # Deliberately opaque: the client shows a generic "something went wrong".
        # Starlette re-raises after this so the traceback still reaches the logs.
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse({"detail": {"error": "internal_error"}}, status_code=500)


def setup_routers(app: FastAPI, fastapi_users: FastAPIUsers) -> None:
    app.include_router(api_router, prefix=settings.API_PATH)
    app.include_router(
        fastapi_users.get_auth_router(
            jwt_authentication,
            requires_verification=False,
        ),
        prefix=f"{settings.API_PATH}/auth/jwt",
        tags=["auth"],
    )
    app.include_router(
        fastapi_users.get_register_router(UserRead, UserCreate),
        prefix=f"{settings.API_PATH}/auth",
        tags=["auth"],
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
    app.mount("/", StaticFiles(directory="static"), name="static")

    @app.middleware("http")
    async def _add_404_middleware(request: Request, call_next):
        """Serves static assets on 404"""
        response = await call_next(request)
        path = request["path"]
        if path.startswith(settings.API_PATH) or path.startswith("/docs"):
            return response
        if response.status_code == 404:
            return FileResponse("static/index.html")
        return response


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
