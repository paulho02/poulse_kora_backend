"""Drop and recreate the entire database schema, then reapply every migration -
a completely fresh, empty database, not just feed content.

Unlike `reset_content.py`, there is no "kept as-is" list: this also erases
every user account, Google/OAuth link, verification state, and
payment/subscription row. What's left afterward is exactly what a brand new
deployment starts with - an empty schema with channels re-seeded, because the
seed migration (`0002_seed_channels`) is idempotent and channels are reference
data, not user content.

`DROP SCHEMA public CASCADE; CREATE SCHEMA public;` rather than deleting rows
or dropping tables one at a time - it's the only approach guaranteed to match
what a fresh `alembic upgrade head` expects to find, with no leftover table
outside the ORM's metadata and no orphaned `alembic_version` row naming a
revision that no longer exists - which is exactly what a squash leaves behind,
so this is the script to run after one. Migrations then run as the exact
subprocess CLAUDE.md documents (`alembic upgrade head`) rather than through
alembic's Python API, so this behaves identically to running it by hand.

Redis is flushed (`FLUSHDB`) rather than pattern-cleared like reset_content.py
does: every key in it, derivable or not, points at data that no longer exists
once Postgres is empty, so nothing is worth preserving. `ensure_group` is
re-run afterward so the operation stream's consumer group exists before the
worker's next tick, rather than relying on its own self-heal.

Bucket media is NOT touched - `app/core/storage.py` has no bucket-wide
list/delete (by design: every caller already knows the key it wants), so
every post/profile-picture/feedback object becomes orphaned. Harmless in dev
(eyeball/clear it via the MinIO console, http://localhost:9001) but worth
knowing before pointing this at a real bucket.

Usage (inside the backend container):
    docker compose exec backend python -m scripts.dangerous.reset_database [--yes]
"""

import argparse
import asyncio
import subprocess
import sys

from sqlalchemy import text

from app.core.config import settings
from app.db import async_engine
from app.feed import service
from app.redis import redis_client


def describe_target() -> str:
    """`host:port/dbname`, for the confirmation prompt.

    Assembled field by field rather than `str(settings.DATABASE_URL)` because
    that carries the password, and this string is printed. `PostgresDsn` is a
    pydantic `MultiHostUrl`, so there is no `.host`/`.port` - only `.hosts()`,
    whose entries leave `port` as None when the URL omits it.
    """
    url = settings.DATABASE_URL
    host = url.hosts()[0]
    return f"{host['host']}:{host['port'] or 5432}{url.path}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--yes", action="store_true", help="Skip the confirmation prompt."
    )
    return parser.parse_args()


async def main():
    args = parse_args()

    target = describe_target()

    if not args.yes:
        confirm = input(
            f"This permanently DROPS THE ENTIRE DATABASE at {target} - every "
            "user, post, and subscription - and rebuilds an empty schema from "
            "migrations. Redis is flushed too. This cannot be undone.\n"
            "Type RESET DATABASE to continue: "
        )
        if confirm != "RESET DATABASE":
            print("Aborted.")
            return

    async with async_engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await async_engine.dispose()

    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=True,
    )

    await redis_client.flushdb()
    await service.ensure_group(redis_client)

    print(f"Database at {target} reset and migrated to head. Redis flushed.")


if __name__ == "__main__":
    asyncio.run(main())
