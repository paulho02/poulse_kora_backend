from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings

# `hide_parameters`: a StatementError's text otherwise ends in the bound parameters
# of the statement that failed, and the request logger writes that text into
# `http.request_failed` - for a failed `users` INSERT that is the email and the
# password hash, the one place the "ids, never contents" logging rule was broken.
# The parameters are still on `exc.params` for a debugger; they just stop being in
# the message.
async_engine = create_async_engine(
    str(settings.ASYNC_DATABASE_URL), pool_pre_ping=True, hide_parameters=True
)

async_session_maker = async_sessionmaker(
    async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    id: Any
