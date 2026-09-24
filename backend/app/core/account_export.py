"""Handing an account holder everything this service knows about them.

One entry point pair behind `GET /users/me/export` (app/api/users.py): `collect`
gathers the data, `stream_zip` turns it into the bytes of a download. Together
they answer GDPR Art. 15 (access) and Art. 20 (portability) without a human in
the loop - the alternative being a documented manual process with a one-month
deadline, which is a promise somebody has to keep by hand every single time.

**Why a ZIP of real files rather than a JSON body with media links.** Links were
the first design and cannot be made to work honestly:

- A presigned S3 URL cannot live a month. SigV4 caps `X-Amz-Expires` at seven
  days (and `MEDIA_URL_TTL_SECONDS` is an hour), so any "valid until" longer than
  that would be a URL that 403s long before the deadline the law names. Minting a
  *separate*, longer-lived credential just for exports would mean a second access
  path into the bucket whose only purpose is to outlive the checks the first has.
- A presigned URL carries its own authorization. Whoever holds the string holds
  the bytes, with no account and no token - tolerable for a feed image already
  being shown to strangers, and the wrong shape for a file whose entire purpose
  is to concentrate one person's personal data in one place.
- Art. 20 asks for the data in a "structured, commonly used, machine-readable
  format". A link is a promise that the data still exists somewhere else; a file
  is the data. The point of portability is that the copy survives the account it
  came from - including the case where the very next thing the user does is
  delete that account.

So the download contains the bytes. Five things are load-bearing.

**Everything Postgres and Redis know is read *before* the response starts.**
FastAPI closes a `yield` dependency - the DB session, and with it the connection -
when the handler returns, which is before a `StreamingResponse` body is consumed.
A generator that lazily queried the session would therefore pass a test that
awaits the whole body inside the request and fail against a real server. So
`collect` is called in the route, returns plain Python, and `stream_zip` touches
nothing but `app.core.storage`, whose httpx client lives as long as the process.
The transfer is long, but nothing is held open across it except the socket.

**The archive is streamed, never assembled in memory.** One post may carry
`POST_MEDIA_MAX_TOTAL_BYTES` (40 MB) and an account has no cap on posts, so the
finished file has no bound worth naming. `zipfile` writes happily into a
write-only sink - it wraps one in `_Tellable` and falls back to data descriptors -
which is all a chunked HTTP body needs. Peak memory is one object, bounded by the
upload limits, plus the JSON document; never the archive.

**Media is stored, not deflated.** Every object in the bucket is already a JPEG,
a WebP or an H.264 MP4; running DEFLATE over those spends CPU proportional to the
whole export to save approximately nothing. The two text members are deflated,
where it is worth roughly an order of magnitude.

**A media object that cannot be fetched does not fail the export.** The response
began with a 200 long before the first byte of the bucket is read, so there is no
status code left to change - and the JSON, which is the Art. 15 answer, is
complete without it. Failures are collected and written into the archive as
`media/UNAVAILABLE.txt`, so the recipient learns what is missing from the file
itself rather than by counting.

**The JSON is an allow-list, not a dump, and it lives in
app/schemas/account_export.py.** Art. 15 is a right to the personal data plus
context; Art. 20 is narrower still. Neither asks for internal bookkeeping, and
Art. 5(1)(c) points the other way - this file concentrates an account's whole
life into one download that will end up in a Drive folder or an email
attachment, so a field that means nothing to the person is cost without benefit.
Building the document out of explicit models rather than dicts next to the
queries is what keeps that true over time: a column added to `User` reaches the
export only when somebody adds a field to the schema too. What is left out, and
why, is that module's docstring; `export.omitted` carries the same list as stable
codes, because an omission the recipient cannot see is indistinguishable from a
service that does not have the data.
"""

import zipfile
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import trust_service
from app.core.config import settings
from app.core.logger import get_logger
from app.core.storage import StorageError, storage
from app.feed import service
from app.models.channel import Channel
from app.models.channel_subscription import ChannelSubscription
from app.models.feedback import Feedback, FeedbackMedia
from app.models.item import Item
from app.models.oauth_account import OAuthAccount
from app.models.post import Post
from app.models.post_block import PostBlock
from app.models.post_media import PostMedia
from app.models.post_review import PostReview
from app.models.probe_response import ProbeResponse
from app.models.supporter_subscription import SupporterSubscription
from app.models.user import User
from app.models.user_subscription import UserSubscription
from app.schemas.account_export import (
    AccountExportDocument,
    ExportAccount,
    ExportBlock,
    ExportFeedback,
    ExportIdentity,
    ExportItem,
    ExportMediaFile,
    ExportMeta,
    ExportPayment,
    ExportPerk,
    ExportPost,
    ExportProbeResponse,
    ExportReview,
    ExportReviewerTrust,
    ExportSubscription,
    ExportTokens,
)

log = get_logger(__name__)

#: Bumped when the shape of `data.json` changes in a way a reader of an older
#: export would notice. Written into the file itself, so a support request
#: quoting an export can be answered without guessing which version produced it.
FORMAT_VERSION = 1

DATA_MEMBER = "data.json"
README_MEMBER = "README.txt"
MISSING_MEMBER = "media/UNAVAILABLE.txt"


@dataclass(frozen=True)
class ExportMedia:
    """One bucket object, and where it sits inside the archive.

    `path` is what `data.json` points at, so the JSON is navigable offline: every
    media entry names a file that is in the same ZIP next to it.
    """

    path: str
    object_key: str


@dataclass
class AccountExport:
    """Everything the download needs, with nothing left to query."""

    document: AccountExportDocument
    media: list[ExportMedia] = field(default_factory=list)
    locale: str = "en"

    @property
    def filename(self) -> str:
        """`peerkola-export-2026-09-16.zip`.

        Dated rather than sequenced: two exports taken on different days are the
        interesting pair to keep apart, and a counter would need server state
        whose only reader is a filename.
        """
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return f"peerkola-export-{stamp}.zip"


# --- README --------------------------------------------------------------
# Localized here rather than through the `api_error` code contract, for the same
# reason app/core/email_templates.py is: that contract hands a code to the client
# and lets its `.arb` supply the words, and there is no client inside a ZIP file.
# Same shape as the mail and the banner - one dict per locale, resolved
# server-side against the locale the request carried, falling back per key.

_STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "readme": (
            "PEERKOLA - YOUR DATA EXPORT\n"
            "===========================\n"
            "\n"
            "Created: {generated_at}\n"
            "Account: {user_id}\n"
            "\n"
            "This archive is the copy of your personal data you are entitled to\n"
            "under Article 15 (right of access) and Article 20 (right to data\n"
            "portability) of the GDPR. It was produced automatically at your\n"
            "request and has not been seen by anyone else.\n"
            "\n"
            "WHAT IS IN HERE\n"
            "---------------\n"
            "{data_member}\n"
            "    Every record this service holds about you, as UTF-8 JSON: your\n"
            "    account, the channels you subscribe to, the posts you published,\n"
            "    the verdicts you gave other people's posts, your reviewer trust\n"
            "    score, your token balance and any feedback you sent us. Each\n"
            "    section is described by the field names themselves.\n"
            "\n"
            "media/\n"
            "    The files you uploaded, in their stored form: your profile\n"
            "    picture, the images and videos in your posts (including the\n"
            "    poster frames generated for videos), and any screenshots you\n"
            "    attached to feedback. Every one of them is referenced by path\n"
            "    from {data_member}, so you can match a file to the record it\n"
            "    belongs to.\n"
            "\n"
            "    Note that these are not the bytes you uploaded. Images and\n"
            "    videos are re-encoded on arrival and their EXIF metadata -\n"
            "    including any GPS coordinates - is removed before storage, so\n"
            "    what is here is what we actually hold.\n"
            "\n"
            "WHAT IS NOT IN HERE\n"
            "-------------------\n"
            "This file holds what tells you something about yourself, and leaves\n"
            "out five things. They are listed in {data_member} as well, under\n"
            "export.omitted, so you can see that they were left out on purpose:\n"
            "\n"
            "credentials\n"
            "    Your password hash, and the access and refresh tokens if you\n"
            "    sign in with Google. They are about you, but a copy of them in\n"
            "    a file you carry around is a way into your account rather than\n"
            "    information about it.\n"
            "\n"
            "internal_flags\n"
            "    System bookkeeping: whether your email is marked verified, when\n"
            "    a database row was last touched, internal counters and version\n"
            "    numbers, the triage label we put on a feedback report. None of\n"
            "    it describes you, and some of it changes when you do nothing.\n"
            "\n"
            "transient_queue_state\n"
            "    Which posts happen to be waiting in your feed at this second.\n"
            "    The app is already showing you that, and it will have changed\n"
            "    by the time you open this file.\n"
            "\n"
            "post_vote_counts\n"
            "    How many people forwarded or dropped each of your posts. That\n"
            "    is other people's decisions, counted, and nothing in the app\n"
            "    shows it either.\n"
            "\n"
            "other_peoples_posts\n"
            "    Where you reviewed a post, the record of your verdict is here -\n"
            "    that is your data - but the post itself belongs to whoever\n"
            "    wrote it.\n"
            "\n"
            "If you want any of it anyway, ask at {contact} and you will get\n"
            "it: this is a judgement about what is useful, not a refusal.\n"
            "\n"
            "QUESTIONS\n"
            "---------\n"
            "Write to {contact} from the address this account uses.\n"
        ),
        "missing": (
            "Some uploaded files could not be read from storage while this export\n"
            "was being written, and are missing from the media/ folder.\n"
            "Everything else in this archive is complete. Please request a new\n"
            "export, or write to {contact} if the same files are missing again.\n"
            "\n"
            "Missing:\n"
            "{paths}\n"
        ),
        "trust_explanation": (
            "How carefully you read decides how far the posts you forward "
            "travel. It is measured only over the trailing window named above - "
            "nothing older is kept - and it affects forwards only, never the "
            "reach of a post you publish yourself."
        ),
    },
    "de": {
        "readme": (
            "PEERKOLA - DEIN DATENEXPORT\n"
            "===========================\n"
            "\n"
            "Erstellt: {generated_at}\n"
            "Konto:    {user_id}\n"
            "\n"
            "Dieses Archiv ist die Kopie deiner personenbezogenen Daten, die dir\n"
            "nach Artikel 15 (Auskunftsrecht) und Artikel 20 (Recht auf\n"
            "Datenübertragbarkeit) der DSGVO zusteht. Es wurde auf deine\n"
            "Anfrage hin automatisch erstellt und von niemandem sonst gesehen.\n"
            "\n"
            "WAS DRIN IST\n"
            "------------\n"
            "{data_member}\n"
            "    Alle Daten, die dieser Dienst über dich gespeichert hat, als\n"
            "    UTF-8-JSON: dein Konto, deine abonnierten Kanäle, deine\n"
            "    veröffentlichten Beiträge, deine Bewertungen fremder\n"
            "    Beiträge, dein Reviewer-Trust-Score, dein Token-Guthaben und\n"
            "    dein eingesendetes Feedback. Die Feldnamen beschreiben sich\n"
            "    selbst.\n"
            "\n"
            "media/\n"
            "    Deine hochgeladenen Dateien in gespeicherter Form: dein\n"
            "    Profilbild, die Bilder und Videos deiner Beiträge (samt der\n"
            "    für Videos erzeugten Vorschaubilder) und Screenshots aus\n"
            "    deinem Feedback. Jede Datei ist per Pfad aus {data_member}\n"
            "    referenziert, du kannst sie also dem passenden Datensatz\n"
            "    zuordnen.\n"
            "\n"
            "    Das sind nicht die Bytes, die du hochgeladen hast: Bilder und\n"
            "    Videos werden beim Upload neu kodiert, und ihre EXIF-Metadaten\n"
            "    - einschließlich GPS-Koordinaten - werden vor dem Speichern\n"
            "    entfernt. Hier steht, was wir tatsächlich haben.\n"
            "\n"
            "WAS NICHT DRIN IST\n"
            "------------------\n"
            "Diese Datei enthält das, was dir etwas über dich sagt, und lässt\n"
            "fünf Dinge weg. Sie stehen auch in {data_member} unter\n"
            "export.omitted, damit du siehst, dass sie bewusst fehlen:\n"
            "\n"
            "credentials\n"
            "    Dein Passwort-Hash und, falls du dich mit Google anmeldest,\n"
            "    die Access- und Refresh-Tokens. Sie betreffen dich, aber eine\n"
            "    Kopie davon in einer Datei, die du herumträgst, ist ein Weg in\n"
            "    dein Konto und keine Information darüber.\n"
            "\n"
            "internal_flags\n"
            "    Interne Verwaltung: ob deine E-Mail als bestätigt markiert\n"
            "    ist, wann eine Datenbankzeile zuletzt angefasst wurde, interne\n"
            "    Zähler und Versionsnummern, unser Bearbeitungsstatus zu einem\n"
            "    Feedback. Nichts davon beschreibt dich, und manches ändert\n"
            "    sich, ohne dass du etwas tust.\n"
            "\n"
            "transient_queue_state\n"
            "    Welche Beiträge gerade in deinem Feed warten. Das zeigt dir\n"
            "    die App ohnehin, und es hat sich geändert, bevor du diese\n"
            "    Datei öffnest.\n"
            "\n"
            "post_vote_counts\n"
            "    Wie oft deine Beiträge weitergeleitet oder verworfen wurden.\n"
            "    Das sind die Entscheidungen anderer Leute, gezählt, und die\n"
            "    App zeigt sie ebenfalls nicht.\n"
            "\n"
            "other_peoples_posts\n"
            "    Wo du einen Beitrag bewertet hast, ist deine Bewertung\n"
            "    enthalten - das sind deine Daten - aber der Beitrag selbst\n"
            "    gehört der Person, die ihn geschrieben hat.\n"
            "\n"
            "Wenn du das trotzdem haben möchtest, schreib an {contact}, dann\n"
            "bekommst du es: Das ist eine Einschätzung, was nützlich ist, keine\n"
            "Verweigerung.\n"
            "\n"
            "FRAGEN\n"
            "------\n"
            "Schreib an {contact} von der E-Mail-Adresse dieses Kontos aus.\n"
        ),
        "missing": (
            "Einige hochgeladene Dateien konnten beim Erstellen dieses Exports\n"
            "nicht aus dem Speicher gelesen werden und fehlen im Ordner media/.\n"
            "Alles andere in diesem Archiv ist vollständig. Fordere bitte einen\n"
            "neuen Export an oder schreib an {contact}, falls dieselben Dateien\n"
            "erneut fehlen.\n"
            "\n"
            "Fehlend:\n"
            "{paths}\n"
        ),
        "trust_explanation": (
            "Wie sorgfältig du liest, entscheidet darüber, wie weit die von "
            "dir weitergeleiteten Beiträge reisen. Gemessen wird nur über den "
            "oben genannten Zeitraum - älteres wird nicht aufbewahrt - und es "
            "wirkt sich nur auf Weiterleitungen aus, nie auf die Reichweite "
            "eines Beitrags, den du selbst veröffentlichst."
        ),
    },
}


def _t(locale: str, key: str, **params: object) -> str:
    """One string, resolved to `locale`, falling back per key to DEFAULT_LOCALE.

    Per *key* rather than per locale, exactly as in email_templates: a
    half-finished translation then degrades to one English paragraph inside a
    German README, instead of raising KeyError and failing a download somebody is
    legally entitled to.
    """
    table = _STRINGS.get(locale, {})
    template = table.get(key) or _STRINGS[settings.DEFAULT_LOCALE][key]
    return template.format(**params) if params else template


# --- collection ----------------------------------------------------------

#: What the file deliberately does not carry, as stable codes. The words are in
#: the README (localized), and the reasoning is the docstring of
#: app/schemas/account_export.py - this is the join between the two.
OMITTED = (
    "credentials",
    "internal_flags",
    "transient_queue_state",
    "post_vote_counts",
    "other_peoples_posts",
)

#: Named in the export rather than the processor's own customer id, which
#: identifies the account *there* and which a request to them would not need.
PAYMENT_PROCESSOR = "Mollie"


def _basename(object_key: str) -> str:
    """The file name a bucket key ends in: `post-media/<uuid>.jpg` gives
    `<uuid>.jpg`.

    Safe as an archive member because post and feedback keys are random UUIDs
    under a flat prefix (see app/core/storage.py) - the uniqueness is the key's,
    not something this function imposes. Dropping everything before the last
    separator also means no key can place a member outside `media/`.
    """
    return object_key.rsplit("/", 1)[-1]


def _attach(media: list[ExportMedia], folder: str, object_key: str) -> str:
    """Queue a bucket object for the archive; return the path it will sit at.

    One function for it so a path is only ever written once - the JSON pointing
    at a member that is not in the ZIP is the one inconsistency this file cannot
    survive, and it is exactly what two independently built strings eventually
    produce.
    """
    path = f"{folder}/{_basename(object_key)}"
    media.append(ExportMedia(path=path, object_key=object_key))
    return path


async def collect(
    session: AsyncSession, redis: Redis, user: User, *, locale: str
) -> AccountExport:
    """Read everything held about `user` into one document.

    Every query happens here, and nothing in the returned value refers back to
    the session - see the module docstring for why that is a hard requirement
    rather than a style preference. *What* each section may contain is
    app/schemas/account_export.py, which is an allow-list rather than a
    description of one: a column added to a model reaches this file only when
    somebody adds a field there too.
    """
    user_id = user.id
    media: list[ExportMedia] = []

    profile_picture = None
    if user.profile_picture_key:
        profile_picture = ExportMediaFile(
            type="image",
            file=_attach(media, "media/profile-picture", user.profile_picture_key),
        )

    account = ExportAccount(
        id=str(user_id),
        email=user.email,
        username=user.username,
        bio=user.bio,
        created=user.created,
        content_languages=list(user.content_languages or []),
        dark_mode=user.dark_mode,
        profile_picture=profile_picture,
    )

    # Read through a query even though `user.oauth_accounts` is already loaded
    # (`lazy="selectin"`), so every section here has one shape and none of them
    # depends on which relationships happen to be eager today.
    identities = [
        ExportIdentity(provider=row.oauth_name, account_email=row.account_email)
        for row in (
            await session.execute(
                select(OAuthAccount).where(OAuthAccount.user_id == user_id)
            )
        )
        .scalars()
        .all()
    ]

    subscriptions = [
        ExportSubscription(channel_name=channel_name, subscribed_at=created)
        for channel_name, created in (
            await session.execute(
                select(Channel.name, ChannelSubscription.created)
                .join(Channel, Channel.id == ChannelSubscription.channel_id)
                .where(ChannelSubscription.user_id == user_id)
                .order_by(ChannelSubscription.created)
            )
        ).all()
    ]

    posts = await _collect_posts(session, user_id, media)

    reviews = [
        ExportReview(post_id=post_id, verdict=kind, created=created)
        for post_id, kind, created in (
            await session.execute(
                select(PostReview.post_id, PostReview.kind, PostReview.created)
                .where(PostReview.user_id == user_id)
                .order_by(PostReview.created)
            )
        ).all()
    ]

    probe_responses = [
        ExportProbeResponse(
            post_id=row.post_id,
            expected_verdict=row.expected_kind,
            given_verdict=row.given_kind,
            correct=row.correct,
            created=row.created,
        )
        for row in (
            await session.execute(
                select(ProbeResponse)
                .where(ProbeResponse.user_id == user_id)
                .order_by(ProbeResponse.created)
            )
        )
        .scalars()
        .all()
    ]

    perks = [
        ExportPerk(kind=kind, created=created)
        for kind, created in (
            await session.execute(
                select(UserSubscription.kind, UserSubscription.created)
                .where(UserSubscription.user_id == user_id)
                .order_by(UserSubscription.created)
            )
        ).all()
    ]

    payments = [
        ExportPayment(
            processor=PAYMENT_PROCESSOR,
            status=row.status,
            current_period_end=row.current_period_end,
            created=row.created,
        )
        for row in (
            await session.execute(
                select(SupporterSubscription)
                .where(SupporterSubscription.user_id == user_id)
                .order_by(SupporterSubscription.created)
            )
        )
        .scalars()
        .all()
    ]

    feedback = await _collect_feedback(session, user_id, media)

    items = [
        ExportItem(value=row.value, created=row.created)
        for row in (await session.execute(select(Item).where(Item.user_id == user_id)))
        .scalars()
        .all()
    ]

    # Redis knows two things about a reader that Postgres does not and that are
    # worth carrying: what they can spend, and how the algorithm rates them. The
    # trust score is the one the GDPR is most pointed about - Art. 15(1)(h), the
    # logic behind profiling - so it ships with the window it was measured over
    # and a sentence saying what it does, rather than as a bare number nobody
    # could act on. The third thing Redis knows, the review queue, is left out:
    # it changes minute to minute, and the app is showing it to them as they
    # read this.
    trust = await trust_service.reviewer_trust(session, redis, str(user_id))

    document = AccountExportDocument(
        export=ExportMeta(
            service=settings.PROJECT_NAME,
            format_version=FORMAT_VERSION,
            generated_at=datetime.now(timezone.utc),
            subject_id=str(user_id),
            legal_basis=["GDPR Art. 15", "GDPR Art. 20"],
            omitted=list(OMITTED),
        ),
        account=account,
        identities=identities,
        channel_subscriptions=subscriptions,
        posts=posts,
        reviews=reviews,
        probe_responses=probe_responses,
        perks=perks,
        payments=payments,
        feedback=feedback,
        items=items,
        tokens=ExportTokens(balance=await service.token_balance(redis, str(user_id))),
        reviewer_trust=ExportReviewerTrust(
            score=trust.score,
            band=trust.band,
            reach_multiplier=trust.reach_multiplier,
            measured_over_days=trust.window_days,
            explanation=_t(locale, "trust_explanation"),
        ),
    )
    return AccountExport(document=document, media=media, locale=locale)


async def _collect_posts(
    session: AsyncSession, user_id: UUID, media: list[ExportMedia]
) -> list[ExportPost]:
    """The posts this account published, blocks and attachments included.

    Three flat queries rather than a relationship walk: blocks and media are
    keyed by post id and reassembled in Python, which keeps this readable as
    "rows in, models out" and keeps the identity map from holding every post an
    account ever wrote plus its whole object graph.
    """
    rows = (
        (
            await session.execute(
                select(Post, Channel.name)
                .join(Channel, Channel.id == Post.channel_id)
                .where(Post.author_id == user_id)
                .order_by(Post.created)
            )
        )
        .tuples()
        .all()
    )
    if not rows:
        return []

    post_ids = [post.id for post, _ in rows]
    media_rows = (
        (
            await session.execute(
                select(PostMedia).where(PostMedia.post_id.in_(post_ids))
            )
        )
        .scalars()
        .all()
    )
    block_rows = (
        (
            await session.execute(
                select(PostBlock)
                .where(PostBlock.post_id.in_(post_ids))
                .order_by(PostBlock.post_id, PostBlock.position)
            )
        )
        .scalars()
        .all()
    )

    media_by_id: dict[int, ExportMediaFile] = {}
    for row in media_rows:
        folder = f"media/posts/{row.post_id}"
        media_by_id[row.id] = ExportMediaFile(
            type=row.media_type,
            file=_attach(media, folder, row.object_key),
            poster_file=(
                _attach(media, folder, row.poster_object_key)
                if row.poster_object_key
                else None
            ),
        )

    blocks_by_post: dict[int, list[ExportBlock]] = {}
    for row in block_rows:
        blocks_by_post.setdefault(row.post_id, []).append(
            ExportBlock(
                position=row.position,
                type=row.block_type,
                text=row.text if row.block_type == "text" else None,
                media=media_by_id.get(row.media_id) if row.media_id else None,
            )
        )

    return [
        ExportPost(
            id=post.id,
            channel_name=channel_name,
            language=post.language,
            is_anonymous=post.is_anonymous,
            created=post.created,
            blocks=blocks_by_post.get(post.id, []),
        )
        for post, channel_name in rows
    ]


async def _collect_feedback(
    session: AsyncSession, user_id: UUID, media: list[ExportMedia]
) -> list[ExportFeedback]:
    """Feedback still linked to this account.

    Anonymous submissions are absent and cannot be otherwise: they store no
    `user_id` at all (see app/models/feedback.py), which is the whole point of
    the option - there is nothing to match them by, here or anywhere else.
    """
    rows = (
        (
            await session.execute(
                select(Feedback)
                .where(Feedback.user_id == user_id)
                .order_by(Feedback.created)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return []

    feedback_ids = [row.id for row in rows]
    attachments: dict[int, list[ExportMediaFile]] = {}
    for row in (
        (
            await session.execute(
                select(FeedbackMedia).where(FeedbackMedia.feedback_id.in_(feedback_ids))
            )
        )
        .scalars()
        .all()
    ):
        attachments.setdefault(row.feedback_id, []).append(
            ExportMediaFile(
                type=row.media_type,
                file=_attach(
                    media, f"media/feedback/{row.feedback_id}", row.object_key
                ),
            )
        )

    return [
        ExportFeedback(
            id=row.id,
            kind=row.kind,
            message=row.message,
            rating=row.rating,
            allow_contact=row.allow_contact,
            consented_at=row.user_agreed_data_saving_at,
            created=row.created,
            media=attachments.get(row.id, []),
        )
        for row in rows
    ]


# --- streaming -----------------------------------------------------------


class _Sink:
    """A write-only file object that hands each write straight back out.

    `zipfile` needs somewhere to write; an HTTP body needs something to yield.
    This is the join. It deliberately implements neither `seek` nor `tell`, which
    is what makes `ZipFile` treat it as a non-seekable stream and emit data
    descriptors instead of rewinding to patch each local header - the only mode
    in which a ZIP can be produced without knowing the whole file first.
    """

    def __init__(self) -> None:
        self._chunks: list[bytes] = []

    def write(self, data: bytes) -> int:
        self._chunks.append(bytes(data))
        return len(data)

    def flush(self) -> None:  # pragma: no cover - part of the file protocol
        pass

    def drain(self) -> bytes:
        data = b"".join(self._chunks)
        self._chunks.clear()
        return data


async def stream_zip(export: AccountExport) -> AsyncIterator[bytes]:
    """Yield the bytes of the download, a chunk at a time.

    Ordered README, JSON, then media, so a recipient who opens the archive while
    it is still arriving sees the explanation first. The JSON already names every
    media path, so it does not have to come after the files it describes.
    """
    sink = _Sink()
    missing: list[str] = []
    written = 0

    meta = export.document.export
    with zipfile.ZipFile(sink, "w", allowZip64=True) as archive:
        readme = _t(
            export.locale,
            "readme",
            generated_at=meta.generated_at.isoformat(),
            user_id=meta.subject_id,
            data_member=DATA_MEMBER,
            contact=settings.SUPPORT_EMAIL,
        )
        archive.writestr(README_MEMBER, readme, zipfile.ZIP_DEFLATED)
        # The model serializes itself, so what lands in the file is exactly the
        # schema's field list - there is no dict in between that could carry
        # something the schema never declared.
        #
        # `exclude_none` because a null here is never an answer: a text block has
        # no `media`, a photo has no `poster_file`, and printing both as null on
        # every entry is most of the file. The sections themselves are lists and
        # so always present - an empty one is the "we hold none of these" that
        # Art. 15 actually asks for, and that is the only absence with meaning.
        archive.writestr(
            DATA_MEMBER,
            export.document.model_dump_json(indent=2, exclude_none=True),
            zipfile.ZIP_DEFLATED,
        )
        chunk = sink.drain()
        if chunk:
            yield chunk

        for entry in export.media:
            try:
                data = await storage.get_object(entry.object_key)
            except StorageError:
                # Not fatal, and not raised: the status line went out long ago
                # (see the module docstring). Recorded in the archive instead.
                log.warning(
                    "account.export_media_unavailable", object_key=entry.object_key
                )
                missing.append(entry.path)
                continue
            # ZIP_STORED: the bytes are already a compressed image or an H.264
            # clip, so deflating them costs CPU proportional to the whole export
            # and saves nothing.
            archive.writestr(entry.path, data, zipfile.ZIP_STORED)
            written += 1
            chunk = sink.drain()
            if chunk:
                yield chunk

        if missing:
            archive.writestr(
                MISSING_MEMBER,
                _t(
                    export.locale,
                    "missing",
                    contact=settings.SUPPORT_EMAIL,
                    paths="\n".join(missing),
                ),
                zipfile.ZIP_DEFLATED,
            )

    # The central directory is written by `ZipFile.close()`, on the way out of
    # the `with` - so this last drain is what makes the file a ZIP rather than a
    # run of members.
    chunk = sink.drain()
    if chunk:
        yield chunk

    log.info(
        "account.exported",
        user_id=meta.subject_id,
        posts=len(export.document.posts),
        reviews=len(export.document.reviews),
        media_files=written,
        media_missing=len(missing),
    )
