"""Application logging: one configuration, one line shape, one request id.

Everything this process emits — our own lines, uvicorn's, SQLAlchemy's, a
library's warning — leaves through a single stdout handler configured here, in
one of two formats:

- **console** (local dev): a compact human line, colourless, with the structured
  fields appended as `key=value`.
- **json** (deployed): one JSON object per line. Railway parses JSON on stdout
  and turns each *top-level* key into a filterable attribute in the log explorer
  (`@level:ERROR`, `@request_id:"a1b2..."`, `@post_id:42`), which is the entire
  reason fields are flattened into the payload rather than nested under a
  `fields` object. `LOG_FORMAT=auto` (the default) picks json whenever the
  process is running on Railway and console otherwise, so neither environment
  needs to be told.

**The contract for a log call.** The message is a stable, dotted *event name* —
`post.created`, `auth.login_failed`, `feed.op_abandoned` — and everything
variable is a keyword:

    log = get_logger(__name__)
    log.info("post.created", post_id=post.id, channel_id=channel.id, price=price)

Not prose with values interpolated into it. Two reasons, and they are the whole
point of the exercise: an event name is countable (an error-rate or
"posts per hour" panel is a filter on one field, not a regex over English), and
the values stay machine-readable instead of being cooked into a sentence. The
keywords are collected into `record.fields` by `StructuredLogger`, so no call
site can accidentally collide with a `LogRecord` attribute the way a bare
`extra={"name": ...}` would.

**Request context rides along without being passed.** `RequestLoggingMiddleware`
(app/core/request_logging.py) opens a context holding the request id, method,
path and — once a route's user dependency resolves — the user id, and every
record emitted anywhere underneath picks it up via `ContextFilter`. So a
traceback raised four calls deep in `app/feed/service.py` carries the id the
client was handed in its `X-Request-ID` header and in the body of its 500. That
is what "production errors should be easily traceable" reduces to in practice:
the user reports an id, and `@request_id:"..."` returns every line that request
produced, in order, across every module.

The context is a *mutable* dict deliberately. A dependency binding `user_id`
after the middleware has already opened the context has to be visible to the
middleware's own access-log line, which is written after the response — with an
immutable context that update would be invisible one frame up.

**Volume.** Logs are cheap until they aren't, and the failure mode is not cost,
it is a signal nobody can find. The rules this codebase follows:

- INFO is for events that happen *once per user action* (a post created, a
  review, a subscription paid) plus one line per request. So volume scales with
  traffic, not with work: a fan-out that touches 3 recipients is one line, not
  four.
- Anything that fires per *retry*, per *item*, or per *poll* is DEBUG —
  `feed.op_parked` is the clearest case, since a parked op re-parks every
  `FEED_RETRY_INTERVAL_SECONDS` for up to ten days.
- The two endpoints the client polls on a timer (`/health`,
  `/posts/feed/status`) are logged at DEBUG as well (`LOG_QUIET_PATHS`) — at a
  few hundred users those alone would outnumber every other line combined and
  say nothing.
- WARNING means "a human may want to know eventually" (rate limit hit, slow
  request, storage delete failed); ERROR means "something is broken".
- `LOG_LEVEL_OVERRIDES` turns one module up without turning everything up, e.g.
  `{"app.feed": "DEBUG"}` to watch fan-out on a live deploy for ten minutes.

**PII.** Log ids, never contents: no email addresses (use `user_id` — an email
is one join away for whoever legitimately needs it), no post or feedback text,
no tokens, no query strings. `LOG_CLIENT_IP` exists because an IP is personal
data under GDPR: it is on by default because abuse investigation is impossible
without it, and it is one flag to turn off if that trade is not wanted.
"""

import json
import logging
import os
import sys
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from typing import Any

from app.core.config import settings

#: Set once `configure_logging` has run, so the scripts, tests and the app can
#: all call it without fighting over the root handler.
_configured = False

#: Keys the formatters own. A structured field with one of these names is
#: prefixed rather than allowed to overwrite it.
_CORE_KEYS = frozenset(
    {"timestamp", "level", "message", "logger", "error_type", "error_message", "stack"}
)

#: Keyword arguments `logging` itself understands; everything else a call site
#: passes is a structured field.
_LOGGING_KWARGS = frozenset({"exc_info", "stack_info", "stacklevel", "extra"})

#: Third-party loggers that are chatty at the level we want our own code at.
#: Raised (never lowered) here, and each one overridable via LOG_LEVEL_OVERRIDES.
_LIBRARY_LEVELS = {
    "httpx": "WARNING",
    "httpcore": "WARNING",
    "urllib3": "WARNING",
    "asyncio": "WARNING",
    "aiosmtplib": "WARNING",
    "multipart": "WARNING",
    "python_multipart": "WARNING",
    "PIL": "WARNING",
    "google": "WARNING",
    "google_auth_httplib2": "WARNING",
    # Connection pool churn and every emitted statement, respectively. The
    # statement log is behind LOG_SQL because it is genuinely useful when
    # chasing a slow query and unusable the rest of the time.
    "sqlalchemy.pool": "WARNING",
    "sqlalchemy.engine": "WARNING",
}


# --- request context ----------------------------------------------------------

_request_context: ContextVar[dict[str, Any] | None] = ContextVar(
    "request_context", default=None
)


def new_request_context(**fields: Any) -> Token:
    """Open a fresh context (a request, or one worker operation).

    Returns the token to hand back to `reset_request_context` when the unit of
    work ends.
    """
    return _request_context.set(dict(fields))


def bind_request_context(**fields: Any) -> None:
    """Add fields to the context of the unit of work already in progress.

    Mutates in place when there is one, so a value bound by a dependency (the
    user id, resolved after the middleware opened the context) is visible to the
    frames above it. Outside any unit of work — a background task, a script —
    it opens one rather than dropping the fields on the floor.
    """
    context = _request_context.get()
    if context is None:
        _request_context.set(dict(fields))
    else:
        context.update(fields)


def reset_request_context(token: Token) -> None:
    _request_context.reset(token)


def current_request_context() -> dict[str, Any]:
    return dict(_request_context.get() or {})


def get_request_id() -> str | None:
    """The id of the request being served, if any.

    Handed to the client in the `X-Request-ID` response header and in the body of
    a 500 (see app/factory.py), which is what makes a user-reported failure
    findable in the log explorer.
    """
    context = _request_context.get()
    return context.get("request_id") if context else None


class ContextFilter(logging.Filter):
    """Copy the request context onto every record as it is emitted.

    A filter rather than something the formatter reads directly, so a record
    stays self-contained: by the time anything formats it, the context it was
    created in may well be gone.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        context = _request_context.get()
        if context:
            record.context = {k: v for k, v in context.items() if v is not None}
        return True


# --- formatters ---------------------------------------------------------------


def _fields_of(record: logging.LogRecord) -> dict[str, Any]:
    """Context + call-site fields, with `None`s dropped.

    Dropping them is what lets a call site pass an optional value
    unconditionally (`route=_route_path(scope)`) without every line growing a
    `"route": null` — an absent key and a null one read the same to a human and
    filter the same in a log explorer, and only one of them costs bytes on every
    request.
    """
    merged: dict[str, Any] = {}
    merged.update(getattr(record, "context", None) or {})
    merged.update(getattr(record, "fields", None) or {})
    return {k: v for k, v in merged.items() if v is not None}


class JsonFormatter(logging.Formatter):
    """One JSON object per line, flat, `default=str` for anything exotic.

    Flat because that is what a log explorer can index: Railway lifts top-level
    keys into queryable attributes, so `post_id` at the root is filterable while
    the same value nested under `fields` is just text inside a blob.
    """

    def __init__(self, static: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._static = static or {}

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            **self._static,
        }
        for key, value in _fields_of(record).items():
            payload[f"field_{key}" if key in _CORE_KEYS else key] = value
        if record.exc_info and record.exc_info[0] is not None:
            payload["error_type"] = record.exc_info[0].__name__
            payload["error_message"] = str(record.exc_info[1])
            payload["stack"] = self.formatException(record.exc_info)
        elif record.exc_text:
            payload["stack"] = record.exc_text
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """`12:31:07.412 INFO     app.api.posts  post.created  post_id=7 price=3`.

    The same records as the JSON formatter, arranged for a terminal: fields
    trail the event name so the left edge stays scannable.
    """

    def format(self, record: logging.LogRecord) -> str:
        stamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        line = (
            f"{stamp}.{int(record.msecs):03d} {record.levelname:<8} "
            f"{record.name}  {record.getMessage()}"
        )
        fields = _fields_of(record)
        if fields:
            line += "  " + " ".join(f"{k}={v}" for k, v in fields.items())
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        if record.stack_info:
            line += "\n" + self.formatStack(record.stack_info)
        return line


# --- the logger itself --------------------------------------------------------


class StructuredLogger(logging.LoggerAdapter):
    """A `logging.Logger` that also takes structured fields as keywords.

    `log.info("post.created", post_id=7)` rather than
    `log.info("created post %s", 7)` or the stock
    `log.info("post.created", extra={"post_id": 7})`. The keywords are collected
    under one record attribute (`fields`), which is what makes them collision-proof:
    passing `extra={"name": ...}` or `extra={"module": ...}` to stdlib logging
    raises, because those names belong to the record.

    The level methods are redefined rather than inherited, and the event name is
    positional-only, because `LoggerAdapter.info(msg, ...)` forwards to
    `LoggerAdapter.log(level, msg, ...)` — so a field innocently named `level` or
    `msg` would collide with a *parameter* and raise `TypeError` at the call
    site. A logging call must never be the thing that breaks a request, least of
    all over the name of a field. The only names that cannot be fields are
    logging's own four keywords (`exc_info`, `stack_info`, `stacklevel`,
    `extra`), which are passed through to it rather than collected.

    `exc_info=`, `stacklevel=` and `isEnabledFor()` work as they do on a plain
    logger; `stacklevel` defaults so that the record still points at the real
    call site rather than at this class.
    """

    def process(self, msg: Any, kwargs: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        fields = {k: kwargs.pop(k) for k in list(kwargs) if k not in _LOGGING_KWARGS}
        extra = dict(kwargs.get("extra") or {})
        extra["fields"] = {**(self.extra or {}), **extra.get("fields", {}), **fields}
        kwargs["extra"] = extra
        return msg, kwargs

    def _emit(self, level: int, event: str, kwargs: dict[str, Any]) -> None:
        if not self.logger.isEnabledFor(level):
            return
        event, kwargs = self.process(event, kwargs)
        # 3 frames up from `Logger._log`: this method, the level method that
        # called it, and then the code that actually logged.
        kwargs.setdefault("stacklevel", 3)
        self.logger.log(level, event, **kwargs)

    def debug(self, event: str, /, **kwargs: Any) -> None:
        self._emit(logging.DEBUG, event, kwargs)

    def info(self, event: str, /, **kwargs: Any) -> None:
        self._emit(logging.INFO, event, kwargs)

    def warning(self, event: str, /, **kwargs: Any) -> None:
        self._emit(logging.WARNING, event, kwargs)

    def error(self, event: str, /, **kwargs: Any) -> None:
        self._emit(logging.ERROR, event, kwargs)

    def critical(self, event: str, /, **kwargs: Any) -> None:
        self._emit(logging.CRITICAL, event, kwargs)

    def exception(self, event: str, /, **kwargs: Any) -> None:
        """ERROR with the traceback of the exception being handled."""
        kwargs.setdefault("exc_info", True)
        self._emit(logging.ERROR, event, kwargs)

    def log(self, level: int, event: str, /, **kwargs: Any) -> None:
        self._emit(level, event, kwargs)

    def bind(self, **fields: Any) -> "StructuredLogger":
        """A copy that adds `fields` to everything it logs — for a long-lived
        component (a worker consumer) that would otherwise repeat them."""
        return StructuredLogger(self.logger, {**(self.extra or {}), **fields})


def get_logger(name: str, **fields: Any) -> StructuredLogger:
    """The module logger. Call it once at module scope: `log = get_logger(__name__)`."""
    return StructuredLogger(logging.getLogger(name), fields)


# --- configuration ------------------------------------------------------------


def _static_fields() -> dict[str, str]:
    """Fields stamped onto every line, identifying *which* copy of the app wrote it.

    Railway already tags a log line with its service and deployment, so this adds
    only what it does not: which environment the config thinks it is, the commit
    (so a spike can be attributed to a release without opening the deploy list)
    and the replica, since several run at once and their lines interleave.
    """
    fields = {"env": settings.ENVIRONMENT}
    commit = os.getenv("RAILWAY_GIT_COMMIT_SHA")
    if commit:
        fields["release"] = commit[:7]
    replica = os.getenv("RAILWAY_REPLICA_ID")
    if replica:
        fields["replica"] = replica[:8]
    return fields


def running_on_railway() -> bool:
    return bool(
        os.getenv("RAILWAY_ENVIRONMENT_NAME") or os.getenv("RAILWAY_SERVICE_ID")
    )


def resolved_log_format() -> str:
    """"json" or "console"; `LOG_FORMAT=auto` decides by where we are running."""
    fmt = settings.LOG_FORMAT.lower()
    if fmt != "auto":
        return fmt
    return "json" if running_on_railway() else "console"


def build_handler() -> logging.Handler:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter(_static_fields())
        if resolved_log_format() == "json"
        else ConsoleFormatter()
    )
    handler.addFilter(ContextFilter())
    return handler


def configure_logging(force: bool = False) -> None:
    """Install the single stdout handler and the per-logger levels. Idempotent.

    Called from `create_app()`, so it runs *after* uvicorn has installed its own
    logging config (the app is imported by the server, not the other way round) —
    which is why it reclaims the `uvicorn.*` loggers explicitly instead of hoping
    to win a race. Its handlers are dropped and it is left to propagate into
    ours, so uvicorn's startup and error lines end up in the same format as
    everything else rather than as stray plain text in the middle of a JSON
    stream.

    `uvicorn.access` is the exception: silenced, because
    `RequestLoggingMiddleware` writes a strictly better line for the same event
    (duration, request id, user, resolved route). Turning `LOG_ACCESS` off hands
    the job back to uvicorn rather than leaving no access log at all.
    """
    global _configured
    if _configured and not force:
        return

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(build_handler())
    root.setLevel(settings.LOG_LEVEL.upper())

    levels = dict(_LIBRARY_LEVELS)
    if settings.LOG_SQL:
        levels["sqlalchemy.engine"] = "INFO"
    levels.update({k: v.upper() for k, v in settings.LOG_LEVEL_OVERRIDES.items()})
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "gunicorn.error"):
        server_logger = logging.getLogger(name)
        server_logger.handlers.clear()
        server_logger.propagate = True
    logging.getLogger("uvicorn.access").setLevel(
        "WARNING" if settings.LOG_ACCESS else "INFO"
    )

    # `warnings.warn` (a library deprecation, a Pillow decode warning) becomes a
    # `py.warnings` record instead of unstructured text on stderr that no log
    # explorer will ever index.
    logging.captureWarnings(True)

    _configured = True
