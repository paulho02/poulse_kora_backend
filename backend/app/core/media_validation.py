"""Upload validation, re-encoding and metadata stripping for everything users upload.

**Nothing reaches object storage without passing through here**: post attachments
via `process_upload` (before app/api/posts.py spends a token or writes a row),
profile pictures via `process_profile_picture`, and feedback attachments via
`process_feedback_upload`. Two rules hold for all three, and they are the reason
this module exists:

- The client's declared `content_type` is never trusted at face value - only as a
  coarse image/video split. The real check is what Pillow/ffprobe actually make of
  the bytes, and the `content_type` that gets stored is derived from that.
- Embedded metadata - JPEG EXIF GPS tags, mp4/mov container atoms - is stripped
  before storing. For post media that is a hard requirement: this app supports
  anonymous posts, and a photo or video straight off someone's phone routinely
  carries GPS in exactly that metadata, which would deanonymize an "anonymous"
  poster. For an avatar it is a plain privacy default rather than a deanonymization
  guard, but the leak is real either way, and more so now that both are served from
  an object store as presigned URLs anyone holding the link can fetch.

Avatars were the exception until the move to object storage: the route checked a
declared type and a size cap and stored the bytes verbatim. That is what
`process_profile_picture` closes.

Video is fully transcoded rather than remuxed - see `_transcode_video` for why - and
that transcode is also where the two other things a consumer needs come from: the
**center crop to a fixed aspect ratio** (see POST_MEDIA_*_RATIO in
app/core/config.py) and the **poster frame** stored alongside the clip so a feed can
show a real preview instead of a black rectangle before playback starts.

Images take the opposite route on both counts: the *client* crops them (only it can
show the author what the crop is throwing away) and this module merely rejects a
shape that is not one of the two allowed ones. Nothing here ever crops an image
silently - a wrong shape is an error, not something to guess at.

The fixed ratios are the one rule that is **specific to posts** rather than to
uploads in general, which is why `process_feedback_upload` exists beside
`process_upload` instead of calling it: a screenshot has whatever shape the
reporter's screen has, and a screen recording cropped to 4:5 loses the bug.
"""

import asyncio
import io
import json
import math
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from fastapi import UploadFile
from PIL import Image, ImageOps

from app.core.config import settings
from app.core.errors import api_error
from app.core.logger import get_logger

log = get_logger(__name__)

# Blocks decompression-bomb-style images (tiny byte size, huge declared pixel
# dimensions): Pillow refuses to even open something that would decode past this
# many pixels. ~40 MP is generous for any real phone photo.
Image.MAX_IMAGE_PIXELS = 40_000_000

# Process-wide cap on concurrent ffmpeg/ffprobe children. A transcode is hundreds
# of MB of RSS and as many threads as it is given, and nothing else bounds how
# many a verified user can start at once (five clips per post, a post every
# second within the interaction budget). Waiting here costs an upload some
# latency; not waiting cost the whole box. Held around the child's lifetime only,
# so the transcode timeout does not start until the slot is acquired.
_FFMPEG_SLOTS = asyncio.Semaphore(settings.MEDIA_MAX_CONCURRENT_FFMPEG)

# The one container family an upload may be: ffmpeg's `mov` demuxer handles
# mov/mp4/m4a/3gp/3g2/mj2 as one unit, which is exactly the set
# POST_VIDEO_ALLOWED_CONTENT_TYPES admits. Passed as `-f` before every `-i` so the
# input is *declared* rather than sniffed - by default ffmpeg probes the bytes and
# will happily open a "video/mp4" as any of the several hundred formats it knows
# (HLS playlists, `concat` scripts, image sequences), which is where its
# file-read and SSRF history lives. Two flags remove every other demuxer.
_INPUT_FORMAT = "mov"

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

ORIENTATIONS = ("landscape", "portrait")


@dataclass
class ProcessedMedia:
    media_type: str  # "image" | "video"
    content_type: str
    data: bytes
    size_bytes: int
    duration_seconds: float | None
    # Dimensions of the stored bytes, so a client can reserve the right box before
    # it has fetched them. Always populated - the ratio is fixed by construction
    # (validated for images, cropped to it for video), but the pixel size is not.
    width: int
    height: int
    # Video only: a still frame, JPEG, at the same aspect ratio as the clip.
    poster: bytes | None = None
    poster_content_type: str | None = None


def allowed_ratios() -> tuple[float, float]:
    return (settings.POST_MEDIA_LANDSCAPE_RATIO, settings.POST_MEDIA_PORTRAIT_RATIO)


def ratio_for_orientation(orientation: str) -> float:
    return (
        settings.POST_MEDIA_LANDSCAPE_RATIO
        if orientation == "landscape"
        else settings.POST_MEDIA_PORTRAIT_RATIO
    )


def nearest_orientation(ratio: float) -> str:
    """Which of the two allowed shapes `ratio` is closest to.

    Compared in log space, so "how far off" means the same thing in both
    directions - a linear distance would quietly favour the portrait ratio, whose
    numeric value is the smaller of the two.
    """
    landscape, portrait = allowed_ratios()
    to_landscape = abs(math.log(ratio / landscape))
    to_portrait = abs(math.log(ratio / portrait))
    return "landscape" if to_landscape <= to_portrait else "portrait"


def matches_allowed_ratio(width: int, height: int) -> bool:
    if width <= 0 or height <= 0:
        return False
    ratio = width / height
    tolerance = settings.POST_MEDIA_RATIO_TOLERANCE
    return any(abs(ratio / allowed - 1) <= tolerance for allowed in allowed_ratios())


async def process_upload(
    file: UploadFile, orientation: str | None = None
) -> ProcessedMedia:
    """Validate, re-encode and return one uploaded file - or raise a structured
    `api_error`. `file.content_type` is only used to pick image vs. video handling;
    each path independently confirms the bytes actually are what was claimed.

    `orientation` ("landscape"/"portrait") is the shape the author chose for a
    **video** and is the only thing the client gets a say in here; it is ignored for
    images, which arrive already cropped. `None` falls back to whichever allowed
    shape the source is closest to.
    """
    if file.content_type in settings.POST_IMAGE_ALLOWED_CONTENT_TYPES:
        return await _process_image(file)
    if file.content_type in settings.POST_VIDEO_ALLOWED_CONTENT_TYPES:
        return await _process_video(file, orientation)
    raise api_error(400, "post_media_invalid_type")


async def process_feedback_upload(file: UploadFile) -> ProcessedMedia:
    """Validate and re-encode one attachment on a feedback submission.

    Same two rules as everything else in this module - the declared content type is
    only a coarse image/video split, and the stored bytes are re-encoded with their
    metadata dropped - but deliberately **not** `process_upload`, for one reason:
    the two fixed post ratios must not apply here. A screenshot is whatever shape
    the reporter's screen is, so validating it against 4:3/4:5 would reject almost
    every real bug report; and a screen recording center-cropped to 4:5 would have
    the thing being reported cut off the sides. So an image is only stripped and
    downscaled, and a video is transcoded with the crop filter left out
    (`crop_to_ratio=False`).

    The EXIF strip is still worth having even though a screenshot carries none: a
    reporter may well photograph a broken screen with another phone, and a feedback
    row can be submitted anonymously - GPS in that photo would undo the anonymity
    the submitter chose, exactly as it would on an anonymous post.
    """
    if file.content_type in settings.POST_IMAGE_ALLOWED_CONTENT_TYPES:
        return await _process_feedback_image(file)
    if file.content_type in settings.POST_VIDEO_ALLOWED_CONTENT_TYPES:
        data = await file.read()
        if len(data) > settings.FEEDBACK_VIDEO_MAX_BYTES:
            raise api_error(400, "feedback_media_too_large")
        return await process_video_bytes(
            data,
            crop_to_ratio=False,
            max_duration_seconds=settings.FEEDBACK_VIDEO_MAX_DURATION_SECONDS,
            error_prefix="feedback_media",
        )
    raise api_error(400, "feedback_media_invalid_type")


async def _process_feedback_image(file: UploadFile) -> ProcessedMedia:
    """`_process_image` minus the aspect-ratio check - see
    `process_feedback_upload` for why that one rule cannot carry over."""
    data = await file.read()
    if len(data) > settings.FEEDBACK_IMAGE_MAX_BYTES:
        raise api_error(400, "feedback_media_too_large")

    try:
        probe = _open_within_pixel_budget(data)
        probe.verify()
    except Exception:
        raise api_error(400, "feedback_media_invalid_type") from None

    img = Image.open(io.BytesIO(data))  # verify() leaves its parser unusable
    img.load()
    source_format = img.format
    # Applied before it is stripped, so the stored image is the right way up - a
    # phone photo of a broken screen is the case this matters for.
    img = ImageOps.exif_transpose(img) or img

    has_alpha = img.mode in ("RGBA", "LA") or (
        img.mode == "P" and "transparency" in img.info
    )
    # Screenshots are routinely PNG, and a PNG screenshot re-encoded as JPEG picks
    # up ringing around exactly the text and UI edges the reporter is pointing at -
    # so lossless is kept whenever the source was, rather than only for alpha.
    save_format = source_format if source_format in ("PNG", "WEBP") else "JPEG"
    if save_format == "JPEG":
        if img.mode != "RGB":
            img = img.convert("RGB")
    else:
        target_mode = "RGBA" if has_alpha else "RGB"
        if img.mode != target_mode:
            img = img.convert(target_mode)

    max_dim = settings.FEEDBACK_IMAGE_MAX_DIMENSION_PX
    if img.width > max_dim or img.height > max_dim:
        img.thumbnail((max_dim, max_dim), Image.LANCZOS)

    buffer = io.BytesIO()
    # No `exif=`/`icc_profile=` forwarded, and `_strip_info` clears what the
    # encoders would otherwise fall back to - together that is the metadata strip.
    save_kwargs = {"quality": 90} if save_format == "JPEG" else {}
    _strip_info(img).save(buffer, format=save_format, **save_kwargs)
    out = buffer.getvalue()

    return ProcessedMedia(
        media_type="image",
        content_type=_IMAGE_SAVE_FORMAT_CONTENT_TYPE[save_format],
        data=out,
        size_bytes=len(out),
        duration_seconds=None,
        width=img.width,
        height=img.height,
    )


async def process_profile_picture(file: UploadFile) -> tuple[bytes, str]:
    """Validate and re-encode a profile picture, returning `(bytes, content_type)`.

    Avatars used to be the one upload that skipped this module entirely: the route
    checked the *declared* `content_type` and a size cap, then stored the client's
    bytes verbatim under the client's label. That was survivable while the bytes sat
    in Postgres behind an authenticated route, and much less so now that they sit in
    an object store and are handed out as presigned URLs anyone holding the link can
    fetch. Three things it closes, all of which post media already had:

    - **The stored `Content-Type` becomes true by construction** rather than a
      client claim, so nothing can be parked in the bucket under a type it isn't
      and left for a browser's content sniffing to reinterpret.
    - **EXIF is stripped** (nothing is passed to `save`), which for a photo
      straight off a phone means GPS coordinates. An avatar is not anonymous the
      way a post can be, so this is not the deanonymization risk `_process_image`
      guards - it is simply a location leak the user never opted into, on a URL
      that is now shareable.
    - **Pixel dimensions are bounded**, so `PROFILE_PICTURE_MAX_BYTES` stops being
      the only thing standing between a decompression bomb and the decoder.

    Deliberately *not* shared with `_process_image`: that one enforces the two fixed
    post ratios, and an avatar has no such rule - it is already cropped square by
    the client, and a wrong shape here should be displayed, not rejected.
    """
    if file.content_type not in settings.PROFILE_PICTURE_ALLOWED_CONTENT_TYPES:
        raise api_error(400, "profile_picture_invalid_type")

    data = await file.read()
    # Before decoding, so an oversized upload is refused without being parsed.
    if len(data) > settings.PROFILE_PICTURE_MAX_BYTES:
        raise api_error(400, "profile_picture_too_large")

    try:
        probe = _open_within_pixel_budget(data)
        probe.verify()
        img = Image.open(io.BytesIO(data))  # verify() leaves its parser unusable
        img.load()
    except Exception:
        raise api_error(400, "profile_picture_invalid_type") from None

    source_format = img.format
    img = ImageOps.exif_transpose(img) or img

    has_alpha = img.mode in ("RGBA", "LA") or (
        img.mode == "P" and "transparency" in img.info
    )
    save_format = "PNG" if has_alpha and source_format in ("PNG", "WEBP") else "JPEG"
    target_mode = "RGBA" if save_format == "PNG" else "RGB"
    if img.mode != target_mode:
        img = img.convert(target_mode)

    max_dim = settings.PROFILE_PICTURE_MAX_DIMENSION_PX
    if img.width > max_dim or img.height > max_dim:
        img.thumbnail((max_dim, max_dim), Image.LANCZOS)

    buffer = io.BytesIO()
    # No `exif=`/`icc_profile=` forwarded, and `_strip_info` clears what the
    # encoders would otherwise fall back to - together that is the metadata strip.
    _strip_info(img).save(
        buffer, format=save_format, **({"quality": 90} if save_format == "JPEG" else {})
    )
    return buffer.getvalue(), _IMAGE_SAVE_FORMAT_CONTENT_TYPE[save_format]


async def _process_image(file: UploadFile) -> ProcessedMedia:
    data = await file.read()
    if len(data) > settings.POST_IMAGE_MAX_BYTES:
        raise api_error(400, "post_media_too_large")

    try:
        probe = _open_within_pixel_budget(data)
        probe.verify()
    except Exception:
        raise api_error(400, "post_media_invalid_type") from None

    # verify() leaves its parser unusable for anything else - reopen fresh.
    img = Image.open(io.BytesIO(data))
    img.load()

    # Captured before exif_transpose()/convert()/thumbnail(), all of which return a
    # new Image with format=None.
    source_format = img.format

    # Applies (and consumes) the EXIF orientation tag, so width/height below are the
    # dimensions a viewer actually sees. Required, not cosmetic: this module strips
    # EXIF on save, so a phone photo whose "portrait" is really a landscape sensor
    # frame plus a rotate-90 tag would otherwise be stored sideways *and* measured
    # against the wrong ratio.
    img = ImageOps.exif_transpose(img) or img

    if not matches_allowed_ratio(img.width, img.height):
        raise api_error(400, "post_media_invalid_aspect_ratio")

    has_alpha = img.mode in ("RGBA", "LA") or (
        img.mode == "P" and "transparency" in img.info
    )
    # JPEG unless transparency actually needs preserving. The client's cropper
    # renders through a canvas and can only hand back PNG, and a 1440x1080 photo is
    # ~10x larger as PNG than as JPEG - paid for in stored bytes and, more to the
    # point, in the bandwidth of every viewer who fetches it.
    save_format = "JPEG"
    if has_alpha and source_format in ("PNG", "WEBP"):
        save_format = source_format

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
    # No `exif=`/`icc_profile=` forwarded, and `_strip_info` clears what the
    # encoders would otherwise fall back to (a JPEG comment, a PNG ICC profile) -
    # together that is the actual EXIF/ICC strip.
    save_kwargs = {"quality": 90} if save_format == "JPEG" else {}
    _strip_info(img).save(buffer, format=save_format, **save_kwargs)
    out = buffer.getvalue()

    return ProcessedMedia(
        media_type="image",
        content_type=_IMAGE_SAVE_FORMAT_CONTENT_TYPE[save_format],
        data=out,
        size_bytes=len(out),
        duration_seconds=None,
        width=img.width,
        height=img.height,
    )


async def _process_video(file: UploadFile, orientation: str | None) -> ProcessedMedia:
    data = await file.read()
    if len(data) > settings.POST_VIDEO_MAX_BYTES:
        raise api_error(400, "post_media_too_large")
    return await process_video_bytes(data, orientation)


async def process_video_bytes(
    data: bytes,
    orientation: str | None = None,
    *,
    crop_to_ratio: bool = True,
    max_duration_seconds: int | None = None,
    error_prefix: str = "post_media",
) -> ProcessedMedia:
    """The video half of `process_upload`, over bytes already in hand.

    Public because scripts/safe/backfill_post_media.py re-runs this same pipeline over clips
    already sitting in Postgres - the rows uploaded before posters, dimensions
    and the H.264 transcode existed, which is why an old clip shows as a black
    rectangle instead of a preview frame.

    Deliberately does *not* re-check POST_VIDEO_MAX_BYTES: that limit is a rule
    about what a client may upload, and applying it to bytes already accepted
    under an older (or larger) one would make the backfill refuse precisely the
    rows it exists to repair.

    `crop_to_ratio=False` runs the same transcode with the aspect-ratio crop left
    out, for the one caller whose video is not a post: a feedback screen recording
    (`process_feedback_upload`). Everything else the transcode does - H.264/AAC so
    every target can decode it, the metadata strip, faststart, the poster frame -
    is wanted there too; only the crop would destroy the thing being reported.
    `error_prefix` picks which family of error codes the client sees, since
    "post_media_video_too_long" is nonsense on a feedback screen.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        src_path = Path(tmp_dir) / "in"
        src_path.write_bytes(data)

        probe = await _probe_video(src_path)
        if probe is None:
            raise api_error(400, f"{error_prefix}_invalid_type")
        duration, src_width, src_height = probe
        max_duration = (
            settings.POST_VIDEO_MAX_DURATION_SECONDS
            if max_duration_seconds is None
            else max_duration_seconds
        )
        if duration > max_duration:
            raise api_error(400, f"{error_prefix}_video_too_long")

        target_ratio: float | None = None
        if crop_to_ratio:
            if orientation not in ORIENTATIONS:
                orientation = nearest_orientation(src_width / src_height)
            target_ratio = ratio_for_orientation(orientation)

        out_path = Path(tmp_dir) / "out.mp4"
        transcode_started = time.perf_counter()
        await _transcode_video(
            src_path, out_path, target_ratio, error_prefix=error_prefix
        )
        transcode_ms = round((time.perf_counter() - transcode_started) * 1000, 1)
        out_data = out_path.read_bytes()

        # Measured, not computed: the crop and scale filters both round to even
        # pixel counts, so the exact output size is ffmpeg's business, not ours.
        out_probe = await _probe_video(out_path)
        if out_probe is None:
            raise api_error(400, f"{error_prefix}_invalid_type")
        _, width, height = out_probe

        poster = await _extract_poster(out_path, duration)

    # INFO, not DEBUG, and the one per-file line that is: a transcode is the
    # heaviest thing this process does (ffmpeg, in-process, up to
    # _TRANSCODE_TIMEOUT_SECONDS holding a worker), it is bounded by how often
    # someone posts a video rather than by traffic, and the ratio of these to
    # `post.created` is the number that decides whether this ever needs to move
    # out to a queue.
    log.info(
        "media.video_transcoded",
        duration_seconds=round(duration, 2),
        transcode_ms=transcode_ms,
        in_bytes=len(data),
        out_bytes=len(out_data),
        width=width,
        height=height,
        cropped=crop_to_ratio,
        has_poster=poster is not None,
    )
    return ProcessedMedia(
        media_type="video",
        # Always mp4 now, whatever came in: _transcode_video normalizes every
        # upload to H.264/AAC in an mp4 container, so this describes the stored
        # bytes by construction rather than echoing the client's claim.
        content_type="video/mp4",
        data=out_data,
        size_bytes=len(out_data),
        duration_seconds=duration,
        width=width,
        height=height,
        poster=poster,
        poster_content_type="image/jpeg" if poster else None,
    )


async def _run_subprocess(
    *args: str, timeout: float = _FFMPEG_TIMEOUT_SECONDS
) -> tuple[int, bytes, bytes]:
    async with _FFMPEG_SLOTS:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise api_error(400, "post_media_invalid_type") from None
    return proc.returncode, stdout, stderr


def _open_within_pixel_budget(data: bytes) -> Image.Image:
    """`Image.open`, refusing anything past `Image.MAX_IMAGE_PIXELS` outright.

    Pillow itself only *warns* at that number and refuses at twice it, so the
    real ceiling was 80 MP - about 320 MB of RGBA decoded from a 12 MB PNG. A
    global `warnings.simplefilter("error", ...)` would close that, but it is
    process state that anything (pytest's per-test capture, another library)
    can reset without noticing, and a guard that fails open silently is not a
    guard. `open` reads only the header, so the size is known before a single
    pixel is decoded and the check costs nothing. Raises, so it lands in the
    same "not a valid image" refusal as any other undecodable upload.
    """
    img = Image.open(io.BytesIO(data))
    if img.width * img.height > Image.MAX_IMAGE_PIXELS:
        raise ValueError("image exceeds the pixel budget")
    return img


def _strip_info(img: Image.Image) -> Image.Image:
    """Drop the metadata Pillow would otherwise carry from `img.info` into a save.

    Not passing `exif=`/`icc_profile=` to `save` is *not* the whole strip: the
    JPEG encoder falls back to `im.info["comment"]` when no `comment=` is given,
    and the PNG encoder to `im.info["icc_profile"]`. A JPEG COM segment is free
    text that some tools fill with a user name or a file path, so on an anonymous
    post it is as identifying as EXIF. `convert`, `exif_transpose` and
    `thumbnail` all copy `info` across, hence this runs on the final image, right
    before the save.
    """
    for key in ("comment", "icc_profile", "exif", "xmp"):
        img.info.pop(key, None)
    return img


async def _probe_video(path: Path) -> tuple[float, int, int] | None:
    """Confirms `path` decodes as a real video (has a video stream) and returns
    `(duration_seconds, width, height)`, or None if it doesn't look like a video at
    all.

    Width/height come from the stream's *display* dimensions: ffprobe is asked for
    the rotation side data too, and a clip carrying a 90/270 degree rotation is
    reported by its unrotated frame size, which would otherwise be measured (and
    cropped) as landscape when every player shows it as portrait.
    """
    returncode, stdout, _ = await _run_subprocess(
        "ffprobe",
        "-v",
        "error",
        "-f",
        _INPUT_FORMAT,
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_type,width,height:stream_side_data=rotation:format=duration",
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
    streams = info.get("streams") or []
    if not streams:
        return None  # no video stream found
    stream = streams[0]
    try:
        duration = float(info["format"]["duration"])
        width = int(stream["width"])
        height = int(stream["height"])
    except (KeyError, TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None

    for side_data in stream.get("side_data_list") or []:
        rotation = side_data.get("rotation")
        if rotation is not None and abs(int(rotation)) % 180 == 90:
            width, height = height, width
            break

    return duration, width, height


async def _transcode_video(
    src: Path,
    dst: Path,
    target_ratio: float | None,
    *,
    error_prefix: str = "post_media",
) -> None:
    """Re-encode `src` into `dst` as H.264/AAC mp4, center-cropped to
    `target_ratio` (or uncropped, when it is None) and normalized for playback
    everywhere.

    A full transcode, deliberately, rather than the cheaper `-c copy` remux this
    used to do. Phone cameras default to **HEVC/H.265** (iPhone "High
    Efficiency", and many Androids), and no Chromium-based browser will decode
    HEVC in a `<video>` element - such a clip parses far enough to report its
    duration and then yields a 0x0 picture with no decodable frames, i.e. it is
    simply unplayable on web. Copying the streams through preserved exactly that
    problem; H.264 is the one video codec every target actually plays.

    Four things fall out of doing it this way:

    - The aspect-ratio crop is free. Images are cropped by the author in the
      client, which cannot re-encode video at all - so a video's crop happens
      here, in a pass that was already running. `crop` is written as an
      expression over ffmpeg's own `iw`/`ih` rather than numbers computed from a
      probe, so the filter stays correct whatever the source turns out to be, and
      it defaults to a centered crop. `target_ratio=None` drops the crop filter
      entirely and keeps everything else - that is the feedback path, where the
      clip is evidence of a bug rather than a post, and cropping it to one of the
      two feed shapes would cut away the part worth seeing.
    - `-movflags +faststart` moves the `moov` index to the front, so progressive
      HTTP playback works on the first read instead of forcing the player to
      range-seek to the tail of the file first.
    - Scaling to POST_VIDEO_MAX_DIMENSION_PX and capping the bitrate keeps stored
      rows a sane size, which matters more than usual here because the bytes live
      in Postgres (see POST_VIDEO_* in app/core/config.py). The bounding box is
      written as `min(iw, max_dim)` rather than the constant, so `decrease` can
      only ever shrink: a plain `scale=max_dim:max_dim` happily *upscales* an
      already-small clip, spending bitrate and DB bytes on invented pixels.
    - Re-encoding drops *all* source metadata inherently; `-map_metadata -1` stays
      as an explicit belt-and-braces against container atoms (GPS included) being
      carried over, which is what the anonymity rule needs.
    """
    max_dim = settings.POST_VIDEO_MAX_DIMENSION_PX
    # Center-crop to the target shape (min() keeps the crop inside the frame
    # whichever way the source is off), then fit the result inside a max_dim box.
    # force_divisible_by=2 because H.264 requires even dimensions.
    crop_filter = ""
    if target_ratio is not None:
        ratio = f"{target_ratio:.6f}"
        crop_filter = f"crop=w='min(iw,ih*{ratio})':h='min(ih,iw/{ratio})',"
    returncode, _, stderr = await _run_subprocess(
        "ffmpeg",
        "-y",
        "-nostdin",
        "-f",
        _INPUT_FORMAT,
        "-i",
        str(src),
        # Global metadata (where phone GPS lives) *and* per-stream tags: the
        # global flag alone leaves `handler_name`/`encoder` on each stream, a
        # weak device fingerprint that has no business on an anonymous post.
        "-map_metadata",
        "-1",
        "-map_metadata:s:v",
        "-1",
        "-map_metadata:s:a",
        "-1",
        "-threads",
        str(settings.MEDIA_TRANSCODE_THREADS),
        "-vf",
        f"{crop_filter}"
        f"scale=w='min(iw,{max_dim})':h='min(ih,{max_dim})'"
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
        # The client is told only "invalid type", which is unhelpful when the
        # clip plays fine on the phone that shot it - ffmpeg's own last words are
        # the only thing that ever explains why, so keep them.
        log.warning(
            "media.transcode_failed",
            returncode=returncode,
            ffmpeg_stderr=stderr.decode("utf-8", "replace")[-500:],
        )
        raise api_error(400, f"{error_prefix}_invalid_type")


async def _extract_poster(path: Path, duration: float) -> bytes | None:
    """One still frame from the already-transcoded clip, as a small JPEG.

    This is what makes an unplayed video look like a photo rather than a black
    rectangle - the client shows it under the play button and, on a feed card,
    instead of the clip entirely (see PostMedia.poster_url). Taken from the
    *output* file so it is cropped and scaled exactly like the video it stands in
    for, and from a moment slightly into the clip rather than frame 0, which is
    very often a black fade-in frame and would defeat the whole point.

    Non-fatal: a video that transcoded fine but yielded no frame here is still a
    perfectly good video, so this returns None and lets the client fall back to
    its neutral tile rather than rejecting the upload.
    """
    seek = max(0.0, min(1.0, duration / 4))
    max_dim = settings.POST_VIDEO_POSTER_MAX_DIMENSION_PX
    with tempfile.TemporaryDirectory() as tmp_dir:
        out = Path(tmp_dir) / "poster.jpg"
        try:
            returncode, _, stderr = await _run_subprocess(
                "ffmpeg",
                "-y",
                "-nostdin",
                # Before -i: seeks by keyframe without decoding everything up to
                # `seek` first.
                "-ss",
                f"{seek:.3f}",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-vf",
                f"scale=w='min(iw,{max_dim})':h='min(ih,{max_dim})'"
                ":force_original_aspect_ratio=decrease",
                "-q:v",
                str(settings.POST_VIDEO_POSTER_QUALITY),
                str(out),
            )
        except Exception:
            log.warning(
                "media.poster_extraction_failed",
                reason="ffmpeg_error",
                exc_info=True,
            )
            return None
        if returncode != 0 or not out.exists() or out.stat().st_size == 0:
            # Non-fatal by design (see the docstring), which is exactly why it
            # needs a line: the upload succeeds, the client silently shows its
            # neutral tile, and nothing else would ever record that it happened.
            log.warning(
                "media.poster_extraction_failed",
                reason="no_frame",
                ffmpeg_stderr=stderr.decode("utf-8", "replace")[-300:],
            )
            return None
        return out.read_bytes()
