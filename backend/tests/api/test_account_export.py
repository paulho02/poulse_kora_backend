"""`GET /users/me/export` - the GDPR Art. 15 / Art. 20 download.

What matters here is not that the route answers, but that the thing it answers
with is a *file someone can open*: a real ZIP, with the JSON and the media inside
it, and with the credentials left out. Three of these tests exist because the
failure they catch is silent - an archive that unzips but is missing a member, a
password hash that rides along inside a document the user is told to keep, or an
export that mentions media it does not contain.
"""

import io
import json
import zipfile

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account_export import DATA_MEMBER, OMITTED, README_MEMBER
from app.core.config import settings
from app.feed import service
from app.models.user import User
from tests.utils import (
    generate_random_string,
    get_jwt_header,
    make_test_png,
    review,
    subscribe,
)

EXPORT = settings.API_PATH + "/users/me/export"


async def _download(client: AsyncClient, user: User) -> zipfile.ZipFile:
    """Fetch the export and open it as an archive.

    Opening it with `zipfile` rather than inspecting the bytes is the assertion
    that matters most: the archive is written into a non-seekable sink, so the
    central directory and the data descriptors are produced by a code path that
    `zipfile` only takes for streams. A malformed one raises here.
    """
    resp = await client.get(EXPORT, headers=get_jwt_header(user))
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/zip"
    assert "attachment" in resp.headers["content-disposition"]
    return zipfile.ZipFile(io.BytesIO(resp.content))


def _data(archive: zipfile.ZipFile) -> dict:
    return json.loads(archive.read(DATA_MEMBER))


class TestExportAuth:
    async def test_requires_authentication(self, client: AsyncClient):
        resp = await client.get(EXPORT)
        assert resp.status_code == 401


class TestExportArchive:
    async def test_archive_has_the_two_text_members(
        self, client: AsyncClient, create_user
    ):
        user: User = await create_user()
        archive = await _download(client, user)
        assert README_MEMBER in archive.namelist()
        assert DATA_MEMBER in archive.namelist()
        # `testzip` returns the first member whose CRC does not match, which is
        # what a truncated or mis-chunked stream produces.
        assert archive.testzip() is None

    async def test_readme_is_localized_by_accept_language(
        self, client: AsyncClient, create_user
    ):
        user: User = await create_user()
        resp = await client.get(
            EXPORT, headers={**get_jwt_header(user), "Accept-Language": "de"}
        )
        archive = zipfile.ZipFile(io.BytesIO(resp.content))
        readme = archive.read(README_MEMBER).decode()
        assert "DEIN DATENEXPORT" in readme
        assert settings.SUPPORT_EMAIL in readme
        # Decoded as UTF-8 above, so this also pins that the archive member is
        # written as UTF-8 rather than as whatever the process's locale encoding
        # happens to be - which is a difference that only shows up on a German
        # README, and only once it is opened.
        assert "Datenübertragbarkeit" in readme

    async def test_document_describes_the_account(
        self, client: AsyncClient, create_user
    ):
        user: User = await create_user()
        data = _data(await _download(client, user))
        assert data["export"]["format_version"] >= 1
        assert data["export"]["subject_id"] == str(user.id)
        assert data["account"]["email"] == user.email
        assert data["account"]["id"] == str(user.id)

    async def test_credentials_are_nowhere_in_the_file(
        self, client: AsyncClient, create_user
    ):
        """The one assertion that has to be made over the raw bytes.

        A hash appearing anywhere - a field somebody adds later, a nested dump of
        the user row, a debug key - is the failure this guards against, and a
        check of `data["account"]` alone would not see it.
        """
        user: User = await create_user()
        archive = await _download(client, user)
        raw = archive.read(DATA_MEMBER).decode()
        assert user.hashed_password not in raw
        assert "hashed_password" not in raw
        assert "access_token" not in raw


class TestExportContents:
    async def test_posts_reviews_and_subscriptions_are_included(
        self,
        client: AsyncClient,
        db: AsyncSession,
        create_user,
        create_channel,
        create_post,
    ):
        author: User = await create_user()
        channel = await create_channel()
        await subscribe(db, author, channel)
        post = await create_post(channel=channel, author=author, text="mine")

        other = await create_post(channel=channel, text="someone else's")
        await review(db, author, other, "forward")

        data = _data(await _download(client, author))

        assert [p["id"] for p in data["posts"]] == [post.id]
        exported = data["posts"][0]
        assert exported["channel_name"] == channel.name
        assert exported["blocks"][0]["text"] == "mine"

        assert [r["post_id"] for r in data["reviews"]] == [other.id]
        assert data["reviews"][0]["verdict"] == "forward"
        # The verdict is the user's data; the post it was given to is not.
        assert other.id not in [p["id"] for p in data["posts"]]

        # By name, not by id: the id means nothing outside this database, and
        # the name is what the person actually subscribed to.
        assert [s["channel_name"] for s in data["channel_subscriptions"]] == [
            channel.name
        ]

    async def test_media_bytes_are_in_the_archive_at_the_path_the_json_names(
        self,
        client: AsyncClient,
        create_user,
        create_channel,
        create_post,
    ):
        """The link between the two halves of the export.

        A path in `data.json` that is not a member of the archive is exactly the
        bug that made links-instead-of-files unworkable, reappearing inside the
        file - so it is asserted rather than assumed.
        """
        author: User = await create_user()
        channel = await create_channel()
        png = make_test_png()
        post = await create_post(
            channel=channel,
            author=author,
            text="with a picture",
            media=[
                {
                    "media_type": "image",
                    "content_type": "image/png",
                    "size_bytes": len(png),
                    "data": png,
                }
            ],
        )

        archive = await _download(client, author)
        data = _data(archive)

        block = data["posts"][0]["blocks"][1]
        path = block["media"]["file"]
        assert path.startswith(f"media/posts/{post.id}/")
        assert path in archive.namelist()
        assert archive.read(path) == png

    async def test_profile_picture_is_exported(
        self, client: AsyncClient, create_user, default_password
    ):
        user: User = await create_user()
        png = make_test_png()
        upload = await client.put(
            settings.API_PATH + "/users/me/profile-picture",
            files={"file": ("me.png", png, "image/png")},
            headers=get_jwt_header(user),
        )
        assert upload.status_code == 200, upload.text

        archive = await _download(client, user)
        path = _data(archive)["account"]["profile_picture"]["file"]
        assert path in archive.namelist()
        # Re-encoded on upload (EXIF stripped), so the bytes are the stored ones
        # rather than the ones sent - which is what the README says they are.
        assert len(archive.read(path)) > 0

    async def test_redis_state_is_included(
        self, client: AsyncClient, redis, create_user, create_channel, create_post
    ):
        """Token balance and trust score are personal data too.

        Both live only in Redis, so a JSON export built by walking the ORM would
        omit them without anything looking wrong. The review *queue* is
        deliberately not here - see the omissions test below.
        """
        user: User = await create_user()
        post = await create_post(channel=await create_channel())
        await service.earn_token(redis, str(user.id), 4)
        await service.place_post(redis, str(user.id), post.id)

        data = _data(await _download(client, user))
        assert data["tokens"]["balance"] == 4
        trust = data["reviewer_trust"]
        assert 0 <= trust["score"] <= 100
        assert trust["measured_over_days"] == settings.TRUST_WINDOW_DAYS

    async def test_feedback_is_included_but_anonymous_submissions_are_not(
        self, client: AsyncClient, create_user
    ):
        user: User = await create_user()
        message = generate_random_string(30)
        signed_in = await client.post(
            settings.API_PATH + "/feedback",
            data={"kind": "bug", "message": message, "consent": "true"},
            headers=get_jwt_header(user),
        )
        assert signed_in.status_code == 201, signed_in.text

        anonymous_message = generate_random_string(30)
        anonymous = await client.post(
            settings.API_PATH + "/feedback",
            data={
                "kind": "bug",
                "message": anonymous_message,
                "consent": "true",
                "is_anonymous": "true",
            },
            headers=get_jwt_header(user),
        )
        assert anonymous.status_code == 201, anonymous.text

        data = _data(await _download(client, user))
        messages = [entry["message"] for entry in data["feedback"]]
        assert message in messages
        # Not a filter - an anonymous row stores no user_id, so there is nothing
        # to match it by. Asserted so a future "helpful" join cannot undo that.
        assert anonymous_message not in messages


class TestExportOmissions:
    """What the file must *not* contain.

    These are the assertions that keep the export an allow-list. Every one of
    them fails silently in the worst way - nobody notices a field that appeared,
    they notice it a year later in a file somebody forwarded - so each is pinned
    rather than left to the schema being read carefully at review time.
    """

    async def test_account_section_is_exactly_the_declared_fields(
        self, client: AsyncClient, create_user
    ):
        """The regression this whole schema exists for.

        A column added to `User` must not reach the export by itself. If this
        fails because a field was added deliberately, the fix is to add it here
        too - which is the point: the decision gets made rather than inherited.
        """
        user: User = await create_user()
        data = _data(await _download(client, user))
        # A subset rather than equality, because the document is serialized with
        # `exclude_none`: an account with no bio and no picture simply has no
        # such key. What must hold is that nothing turns up *outside* this list.
        assert set(data["account"]) <= {
            "id",
            "email",
            "username",
            "bio",
            "created",
            "content_languages",
            "dark_mode",
            "profile_picture",
        }
        # And that the fields which are never null are all actually there - a
        # subset check alone would be satisfied by an empty object.
        assert {"id", "email", "created", "content_languages", "dark_mode"} <= set(
            data["account"]
        )

    async def test_no_internal_state_anywhere_in_the_document(
        self, client: AsyncClient, create_user, create_channel, create_post
    ):
        """Asserted over the raw text, not per section.

        A flag that reappears usually reappears somewhere nobody was looking -
        nested inside a post, or in a section added later - so this checks the
        whole file rather than the keys of one object.
        """
        user: User = await create_user()
        await create_post(channel=await create_channel(), author=user, text="hi")

        raw = (await _download(client, user)).read(DATA_MEMBER).decode()
        for leaked in (
            "hashed_password",
            "is_active",
            "is_superuser",
            "is_verified",
            "settings_revision",
            "onboarding_completed",
            "updated",
            # The probe template catalogue - see ExportProbeResponse.
            "variant_code",
            # Third-party identifiers, and the internal ids a name replaces.
            "mollie_customer_id",
            "account_id",
            "channel_id",
            # Other people's decisions, counted.
            "forwarded_count",
            "dropped_count",
            # Transient Redis state.
            "queued_post_ids",
        ):
            assert leaked not in raw, leaked

    async def test_the_omissions_are_declared(self, client: AsyncClient, create_user):
        """An omission the recipient cannot see is indistinguishable from a
        service that does not hold the data, so the codes ship in the file and
        the README spells each of them out."""
        user: User = await create_user()
        archive = await _download(client, user)
        assert set(_data(archive)["export"]["omitted"]) == set(OMITTED)

        readme = archive.read(README_MEMBER).decode()
        for code in OMITTED:
            assert code in readme, code


class TestExportRateLimit:
    async def test_budget_is_spent_and_refused(
        self, client: AsyncClient, create_user, monkeypatch
    ):
        monkeypatch.setattr(settings, "ACCOUNT_EXPORT_RATE_LIMIT", 1)
        user: User = await create_user()

        first = await client.get(EXPORT, headers=get_jwt_header(user))
        assert first.status_code == 200
        second = await client.get(EXPORT, headers=get_jwt_header(user))
        assert second.status_code == 429
        assert second.json()["detail"]["error"] == "rate_limited"
        assert "Retry-After" in second.headers

    async def test_its_own_budget_not_the_interaction_one(
        self, client: AsyncClient, redis, create_user
    ):
        """An export must not cost somebody their ability to post, or the other
        way round - which is only visible as two separate Redis keys."""
        user: User = await create_user()
        await client.get(EXPORT, headers=get_jwt_header(user))
        assert await redis.zcard(f"rate:account_export:{user.id}") == 1
        assert await redis.zcard(f"rate:interact:{user.id}") == 0
