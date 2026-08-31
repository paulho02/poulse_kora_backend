"""Post-media upload validation, re-encoding and metadata stripping.

Every uploaded file is fully processed here (`process_upload`) before
app/api/posts.py ever spends a token or writes to Postgres. Two things this closes
that the profile-picture route (app/api/users.py) does not: it never trusts the
client's declared `content_type` at face value (only as a coarse image/video split -
the real check is what Pillow/ffprobe actually make of the bytes), and it strips
embedded location metadata - JPEG EXIF GPS tags, mp4/mov container atoms - before
storing. That matters because this app supports anonymous posts: a photo or video
straight off someone's phone routinely carries GPS in exactly this metadata, which
would deanonymize an "anonymous" poster otherwise.

Video is probed and re-muxed (`ffmpeg -map_metadata -1 -c copy`, no transcode)
rather than fully re-validated the way images are - there is no cheap equivalent of
Pillow's `Image.verify()` for video without either shelling out to ffprobe (already
done here, for the stream/duration check) or pulling in a full decoder. A remux
failure is treated as an invalid upload rather than falling back to a real
transcode, which would be a real CPU/latency cost per upload - see POST_VIDEO_* in
app/core/config.py for the caps this trades off against.
"""

import asyncio
import io
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

from fastapi import UploadFile
from PIL import Image

from app.core.config import settings
from app.core.errors import api_error

# Blocks decompression-bomb-style images (tiny byte size, huge declared pixel
# dimensions): Pillow refuses to even open something that would decode past this
# many pixels. ~40 MP is generous for any real phone photo.
Image.MAX_IMAGE_PIXELS = 40_000_000

_FFMPEG_TIMEOUT_SECONDS = 15
# Transcoding is real work, unlike a probe - a POST_VIDEO_MAX_DURATION_SECONDS
# clip at 1080p can take tens of seconds on a busy box. Generous enough not to
# fail a legitimate upload, still bounded so a pathological file can't pin a
# worker forever.
_TRANSCODE_TIMEOUT_SECONDS = 180

_IMAGE_SAVE_FORMAT_CONTENT_TYPE = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}


@dataclass
class ProcessedMedia:
    media_type: str  # "image" | "video"
    content_type: str
    data: bytes
    size_bytes: int
    duration_seconds: float | None


async def process_upload(file: UploadFile) -> ProcessedMedia:
    """Validate, re-encode and return one uploaded file - or raise a structured
    `api_error`. `file.content_type` is only used to pick image vs. video handling;
    each path independently confirms the bytes actually are what was claimed.
    """
    if file.content_type in settings.POST_IMAGE_ALLOWED_CONTENT_TYPES:
        return await _process_image(file)
    if file.content_type in settings.POST_VIDEO_ALLOWED_CONTENT_TYPES:
        return await _process_video(file)
    raise api_error(400, "post_media_invalid_type")


async def _process_image(file: UploadFile) -> ProcessedMedia:
    data = await file.read()
    if len(data) > settings.POST_IMAGE_MAX_BYTES:
        raise api_error(400, "post_media_too_large")

    try:
        probe = Image.open(io.BytesIO(data))
        probe.verify()
    except Exception:
        raise api_error(400, "post_media_invalid_type") from None

    # verify() leaves its parser unusable for anything else - reopen fresh.
    img = Image.open(io.BytesIO(data))
    img.load()

    # Captured before convert()/thumbnail(), both of which return a new Image with
    # format=None.
    save_format = (
        img.format if img.format in _IMAGE_SAVE_FORMAT_CONTENT_TYPE else "JPEG"
    )

    has_alpha = img.mode in ("RGBA", "LA") or (
        img.mode == "P" and "transparency" in img.info
    )
    if save_format == "JPEG":
        if img.mode != "RGB":
            img = img.convert("RGB")
    else:
        target_mode = "RGBA" if has_alpha else "RGB"
        if img.mode != target_mode:
            img = img.convert(target_mode)

    max_dim = settings.POST_IMAGE_MAX_DIMENSION_PX
    if img.width > max_dim or img.height > max_dim:
        img.thumbnail((max_dim, max_dim), Image.LANCZOS)

    buffer = io.BytesIO()
    # No `exif=`/`icc_profile=` forwarded - that omission is the actual EXIF/ICC
    # strip, since Pillow only writes metadata it's explicitly handed.
    save_kwargs = {"quality": 90} if save_format == "JPEG" else {}
    img.save(buffer, format=save_format, **save_kwargs)
    out = buffer.getvalue()

    return ProcessedMedia(
        media_type="image",
        content_type=_IMAGE_SAVE_FORMAT_CONTENT_TYPE[save_format],
        data=out,
        size_bytes=len(out),
        duration_seconds=None,
    )


async def _process_video(file: UploadFile) -> ProcessedMedia:
    data = await file.read()
    if len(data) > settings.POST_VIDEO_MAX_BYTES:
        raise api_error(400, "post_media_too_large")

    with tempfile.TemporaryDirectory() as tmp_dir:
        src_path = Path(tmp_dir) / "in"
        src_path.write_bytes(data)

        duration = await _probe_duration(src_path)
        if duration is None:
            raise api_error(400, "post_media_invalid_type")
        if duration > settings.POST_VIDEO_MAX_DURATION_SECONDS:
            raise api_error(400, "post_media_video_too_long")

        out_path = Path(tmp_dir) / "out.mp4"
        await _transcode_video(src_path, out_path)
        out_data = out_path.read_bytes()

    return ProcessedMedia(
        media_type="video",
        # Always mp4 now, whatever came in: _transcode_video normalizes every
        # upload to H.264/AAC in an mp4 container, so this describes the stored
        # bytes by construction rather than echoing the client's claim.
        content_type="video/mp4",
        data=out_data,
        size_bytes=len(out_data),
        duration_seconds=duration,
    )


async def _run_subprocess(
    *args: str, timeout: float = _FFMPEG_TIMEOUT_SECONDS
) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise api_error(400, "post_media_invalid_type") from None
    return proc.returncode, stdout, stderr


async def _probe_duration(path: Path) -> float | None:
    """Confirms `path` decodes as a real video (has a video stream) and returns its
    duration in seconds, or None if it doesn't look like a video at all.
    """
    returncode, stdout, _ = await _run_subprocess(
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_type:format=duration",
        "-of",
        "json",
        str(path),
    )
    if returncode != 0:
        return None
    try:
        info = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if not info.get("streams"):
        return None  # no video stream found
    try:
        return float(info["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        return None


async def _transcode_video(src: Path, dst: Path) -> None:
    """Re-encode `src` into `dst` as H.264/AAC mp4, normalized for playback
    everywhere.

    A full transcode, deliberately, rather than the cheaper `-c copy` remux this
    used to do. Phone cameras default to **HEVC/H.265** (iPhone "High
    Efficiency", and many Androids), and no Chromium-based browser will decode
    HEVC in a `<video>` element - such a clip parses far enough to report its
    duration and then yields a 0x0 picture with no decodable frames, i.e. it is
    simply unplayable on web. Copying the streams through preserved exactly that
    problem; H.264 is the one video codec every target actually plays.

    Three things fall out of doing it this way:

    - `-movflags +faststart` moves the `moov` index to the front, so progressive
      HTTP playback works on the first read instead of forcing the player to
      range-seek to the tail of the file first.
    - Scaling to POST_VIDEO_MAX_DIMENSION_PX and capping the bitrate keeps stored
      rows a sane size, which matters more than usual here because the bytes live
      in Postgres (see POST_VIDEO_* in app/core/config.py).
    - Re-encoding drops *all* source metadata inherently; `-map_metadata -1` stays
      as an explicit belt-and-braces against container atoms (GPS included) being
      carried over, which is what the anonymity rule needs.
    """
    max_dim = settings.POST_VIDEO_MAX_DIMENSION_PX
    returncode, _, stderr = await _run_subprocess(
        "ffmpeg",
        "-y",
        "-i",
        str(src),
        "-map_metadata",
        "-1",
        # Fit inside a max_dim box without distorting; force_divisible_by=2
        # because H.264 requires even dimensions.
        "-vf",
        f"scale=w={max_dim}:h={max_dim}"
        ":force_original_aspect_ratio=decrease:force_divisible_by=2",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        str(settings.POST_VIDEO_TARGET_CRF),
        "-maxrate",
        settings.POST_VIDEO_MAX_BITRATE,
        "-bufsize",
        settings.POST_VIDEO_MAX_BITRATE,
        # yuv420p, not whatever the source used: phone clips are often yuvj420p
        # (full-range), which some players render with wrong levels.
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        str(dst),
        timeout=_TRANSCODE_TIMEOUT_SECONDS,
    )
    if returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        raise api_error(400, "post_media_invalid_type")
