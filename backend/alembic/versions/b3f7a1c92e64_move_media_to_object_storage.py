# ruff: noqa: E501,I001
"""move profile pictures and post media out of postgres into object storage

Revision ID: b3f7a1c92e64
Revises: 21e8c71022d0
Create Date: 2026-09-03 10:12:44.000000

Data migration, not just a schema change. Every byte currently sitting in
`users.profile_picture`, `post_media.data` and `post_media.poster` is uploaded to
the media bucket (see app/core/storage.py) and replaced by its object key, and
only then are the byte columns dropped.

The copy runs *inside* the migration on purpose. Splitting it into "add the key
columns" / "run a script" / "drop the byte columns" would be the more usual
shape, but `entrypoint.sh` runs `alembic upgrade head` on every boot, so on
Railway the drop would land automatically the moment the code deployed - before
anyone could run the script in between. Doing it here makes the whole move one
step that either completes or fails: if the bucket is unreachable or
misconfigured, the upload raises, the transaction rolls back, nothing is dropped,
and the deploy fails loudly with the media still in Postgres.

**Before deploying this, the STORAGE_* variables have to be set** (see RAILWAY.md
and env-template) - the migration is the first thing that needs them.

`downgrade` is symmetric and pulls the bytes back out of the bucket, so it is a
real reversal rather than a data-losing one. It does not delete the objects it
read: an aborted rollback that had already emptied the bucket would be
unrecoverable, and orphaned objects are cheap.
"""
import asyncio

import sqlalchemy as sa
from alembic import op

from app.core.storage import (
    POST_MEDIA_PREFIX,
    PROFILE_PICTURE_PREFIX,
    post_media_key,
    profile_picture_key,
    storage,
)

# revision identifiers, used by Alembic.
revision = 'b3f7a1c92e64'
down_revision = '21e8c71022d0'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('users', sa.Column('profile_picture_key', sa.String(), nullable=True))
    op.add_column('post_media', sa.Column('object_key', sa.String(), nullable=True))
    op.add_column('post_media', sa.Column('poster_object_key', sa.String(), nullable=True))

    asyncio.run(_upload_existing(op.get_bind()))

    # Safe to make NOT NULL only after the copy: every row now has a key, and a
    # row that somehow did not would have failed the upload above.
    op.alter_column('post_media', 'object_key', existing_type=sa.String(), nullable=False)

    op.drop_column('post_media', 'poster_content_type')
    op.drop_column('post_media', 'poster')
    op.drop_column('post_media', 'data')
    op.drop_column('users', 'profile_picture_content_type')
    op.drop_column('users', 'profile_picture')


def downgrade():
    op.add_column('users', sa.Column('profile_picture', sa.LargeBinary(), nullable=True))
    op.add_column('users', sa.Column('profile_picture_content_type', sa.String(), nullable=True))
    op.add_column('post_media', sa.Column('data', sa.LargeBinary(), nullable=True))
    op.add_column('post_media', sa.Column('poster', sa.LargeBinary(), nullable=True))
    op.add_column('post_media', sa.Column('poster_content_type', sa.String(), nullable=True))

    asyncio.run(_download_existing(op.get_bind()))

    op.alter_column('post_media', 'data', existing_type=sa.LargeBinary(), nullable=False)

    op.drop_column('post_media', 'poster_object_key')
    op.drop_column('post_media', 'object_key')
    op.drop_column('users', 'profile_picture_key')


# The connection handed to these is a *synchronous* SQLAlchemy connection inside
# the migration's own transaction. Using it from an async function is fine and
# deliberate: nothing else is running on this loop, the calls simply block, and it
# keeps every write in the same transaction alembic will commit or roll back. The
# loop exists only so the one storage client (async, httpx-based) can be reused
# across all uploads instead of one `asyncio.run` per row - which would strand the
# client on a closed event loop after the first.

async def _upload_existing(conn) -> None:
    await storage.ensure_bucket()
    try:
        for user_id, data, content_type in conn.execute(
            sa.text(
                "SELECT id, profile_picture, profile_picture_content_type "
                "FROM users WHERE profile_picture IS NOT NULL"
            )
        ):
            key = profile_picture_key(user_id, content_type or "image/jpeg")
            await storage.put_object(
                key, bytes(data), content_type=content_type or "image/jpeg"
            )
            conn.execute(
                sa.text("UPDATE users SET profile_picture_key = :key WHERE id = :id"),
                {"key": key, "id": user_id},
            )

        for media_id, data, content_type, poster, poster_content_type in conn.execute(
            sa.text(
                "SELECT id, data, content_type, poster, poster_content_type "
                "FROM post_media ORDER BY id"
            )
        ):
            key = post_media_key(content_type)
            await storage.put_object(key, bytes(data), content_type=content_type)

            poster_key = None
            if poster is not None:
                poster_type = poster_content_type or "image/jpeg"
                poster_key = post_media_key(poster_type)
                await storage.put_object(
                    poster_key, bytes(poster), content_type=poster_type
                )

            conn.execute(
                sa.text(
                    "UPDATE post_media SET object_key = :key, "
                    "poster_object_key = :poster_key WHERE id = :id"
                ),
                {"key": key, "poster_key": poster_key, "id": media_id},
            )
    finally:
        await storage.aclose()


async def _download_existing(conn) -> None:
    try:
        for user_id, key in conn.execute(
            sa.text(
                "SELECT id, profile_picture_key FROM users "
                "WHERE profile_picture_key IS NOT NULL"
            )
        ):
            conn.execute(
                sa.text(
                    "UPDATE users SET profile_picture = :data, "
                    "profile_picture_content_type = :content_type WHERE id = :id"
                ),
                {
                    "data": await storage.get_object(key),
                    # The bucket knows the real type; the extension in the key is
                    # what this schema can still reconstruct it from.
                    "content_type": _content_type_from_key(key),
                    "id": user_id,
                },
            )

        for media_id, key, poster_key in conn.execute(
            sa.text(
                "SELECT id, object_key, poster_object_key FROM post_media ORDER BY id"
            )
        ):
            poster = await storage.get_object(poster_key) if poster_key else None
            conn.execute(
                sa.text(
                    "UPDATE post_media SET data = :data, poster = :poster, "
                    "poster_content_type = :poster_content_type WHERE id = :id"
                ),
                {
                    "data": await storage.get_object(key),
                    "poster": poster,
                    "poster_content_type": (
                        _content_type_from_key(poster_key) if poster_key else None
                    ),
                    "id": media_id,
                },
            )
    finally:
        await storage.aclose()


_TYPES_BY_EXTENSION = {
    "jpg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "mp4": "video/mp4",
    "mov": "video/quicktime",
}


def _content_type_from_key(key: str) -> str:
    assert key.startswith((PROFILE_PICTURE_PREFIX, POST_MEDIA_PREFIX))
    return _TYPES_BY_EXTENSION.get(key.rsplit(".", 1)[-1], "application/octet-stream")
