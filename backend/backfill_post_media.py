"""Give videos stored before poster frames existed the metadata newer ones have.

Why this exists: the Flutter client draws a video it cannot preview as a plain
black rectangle (`PostMedia.poster_url == null` -> `ColoredBox(Colors.black87)`),
which is exactly what every clip uploaded before `_extract_poster` landed still
looks like in the feed. Those rows are otherwise fine - the bytes are all there -
so the fix is not a migration but a re-run of the current pipeline over what is
already in Postgres.

Each selected row goes through `process_video_bytes`, the same code path an
upload takes, so a backfilled row comes out indistinguishable from a fresh one:
H.264/AAC mp4 (old clips may still be HEVC, which no Chromium-based browser will
decode), center-cropped to one of the two allowed shapes, measured `width`/
`height`, and a poster frame. Re-transcoding rather than only extracting a
poster is the point - a poster over a clip the browser cannot play would just
trade one broken preview for another, and a poster is cropped like the clip it
stands in for, so the two have to be produced together.

Selects on `poster_object_key IS NULL` (the "is there a poster?" flag), so it is
idempotent: a repaired row is skipped on the next run, and a row whose frame
extraction genuinely yields nothing stays selected and is retried - which is the
right default, since that failure is usually about the clip, not about this
script.

One row at a time, committed per row: these bytes are large, and a run that dies
half way through should keep what it has already fixed. A row that fails is
logged and skipped - one unreadable clip must not strand the rest.

The repaired clip is written to the bucket under a **new** key and the old object
deleted only after the row is committed. A crash in between therefore leaves an
orphan in the bucket rather than a row pointing at an object that is no longer
there - and it also means the presigned URL changes, which is what makes clients
holding the old (unplayable) clip pick up the new one.

Usage (inside the backend container):
    docker compose exec backend python backfill_post_media.py
    docker compose exec backend python backfill_post_media.py --dry-run
"""

import asyncio
import sys

from sqlalchemy import select

from app.core.media_validation import process_video_bytes
from app.core.storage import post_media_key, storage
from app.db import async_session_maker
from app.models.post_media import PostMedia


async def backfill(dry_run: bool = False) -> None:
    async with async_session_maker() as session:
        media_ids = (
            await session.scalars(
                select(PostMedia.id)
                .filter(
                    PostMedia.media_type == "video",
                    PostMedia.poster_object_key.is_(None),
                )
                .order_by(PostMedia.id)
            )
        ).all()

    print(f"{len(media_ids)} video(s) without a poster frame.")
    if dry_run or not media_ids:
        return

    repaired = failed = 0
    for media_id in media_ids:
        async with async_session_maker() as session:
            media = await session.get(PostMedia, media_id)
            if media is None:
                continue
            before = media.size_bytes
            previous_key = media.object_key
            try:
                source = await storage.get_object(previous_key)
                # No orientation passed: nothing recorded what the author chose
                # back then, so `nearest_orientation` picks whichever allowed
                # shape the clip is already closest to - the least destructive
                # reading of a crop that was never made.
                processed = await process_video_bytes(source)

                key = post_media_key(processed.content_type)
                await storage.put_object(
                    key, processed.data, content_type=processed.content_type
                )
                poster_key = None
                if processed.poster and processed.poster_content_type:
                    poster_key = post_media_key(processed.poster_content_type)
                    await storage.put_object(
                        poster_key,
                        processed.poster,
                        content_type=processed.poster_content_type,
                    )
            except Exception as exc:  # noqa: BLE001 - one bad clip must not stop the run
                failed += 1
                print(f"  media {media_id}: FAILED ({exc})")
                continue

            media.object_key = key
            media.size_bytes = processed.size_bytes
            media.content_type = processed.content_type
            media.duration_seconds = processed.duration_seconds
            media.width = processed.width
            media.height = processed.height
            media.poster_object_key = poster_key
            await session.commit()

            await storage.delete_object(previous_key)

            repaired += 1
            poster = (
                f"{len(processed.poster)} B"
                if processed.poster
                else "none (extraction found no frame)"
            )
            print(
                f"  media {media_id}: {processed.width}x{processed.height}, "
                f"{before} -> {processed.size_bytes} B, poster {poster}"
            )

    print(f"Done: {repaired} repaired, {failed} failed.")
    await storage.aclose()


if __name__ == "__main__":
    asyncio.run(backfill(dry_run="--dry-run" in sys.argv))
