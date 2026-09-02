"""Byte-range serving for in-memory blobs.

Exists for `GET /posts/{post_id}/media/{media_id}` (app/api/posts.py): Flutter's
`video_player` uses native ExoPlayer/AVPlayer, both of which probe playable URLs
with `Range` requests before and while streaming. Since post-media bytes are
already fully loaded into memory by the time this runs (see POST_VIDEO_* in
app/core/config.py - there is no true streaming from Postgres), this only slices an
in-memory buffer; it does not make the underlying storage itself stream.
"""

import re

from starlette.requests import Request
from starlette.responses import Response

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


def build_range_response(request: Request, content: bytes, media_type: str) -> Response:
    total = len(content)
    range_header = request.headers.get("range")
    if not range_header:
        return Response(
            content=content,
            media_type=media_type,
            headers={"Accept-Ranges": "bytes", "Content-Length": str(total)},
        )

    match = _RANGE_RE.match(range_header.strip())
    if not match or total == 0:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{total}"})

    start_str, end_str = match.groups()
    if start_str == "" and end_str == "":
        return Response(status_code=416, headers={"Content-Range": f"bytes */{total}"})

    if start_str == "":
        # Suffix form ("bytes=-500"): last N bytes.
        length = int(end_str)
        start = max(total - length, 0)
        end = total - 1
    else:
        start = int(start_str)
        end = int(end_str) if end_str != "" else total - 1

    if start > end or start >= total:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{total}"})
    end = min(end, total - 1)

    chunk = content[start : end + 1]
    return Response(
        content=chunk,
        status_code=206,
        media_type=media_type,
        headers={
            "Accept-Ranges": "bytes",
            "Content-Range": f"bytes {start}-{end}/{total}",
            "Content-Length": str(len(chunk)),
        },
    )
