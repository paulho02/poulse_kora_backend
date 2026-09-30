"""The shape of `data.json` inside the account export - and therefore the
allow-list of what leaves this service.

Its own module, and explicit models rather than dicts built from ORM rows,
because of the failure mode that has exactly one cure. A dict assembled next to a
query drifts toward `everything the table has`: the column is right there, adding
it is free, and nobody reviewing a later migration thinks about a file this row
is copied into. A model makes the field list the thing you have to edit, so a new
column on `User` reaches the export only when somebody decides it should.

**GDPR asks for less than a table dump, and asks for something else as well.**
Art. 15 is a right to *the personal data* plus context (purposes, recipients,
retention, the logic behind any profiling). Art. 20 is narrower still: data the
person *provided*, in a portable format. Neither asks for internal bookkeeping,
and Art. 5(1)(c) points the other way - this file concentrates one account's
whole life into a single download that will end up in a Drive folder or an email
attachment, so every field that means nothing to the person is cost without
benefit.

So the rule applied here is: **a field earns its place by telling the person
something about themselves.** Four kinds of thing therefore do not appear, and
`ExportMeta.omitted` names each of them as a code so the omission is visible
rather than silent (the README says the same in words):

- `credentials` - the password hash, and OAuth access/refresh tokens. Personal
  data, and a copy of them in a portable file is a way into the account rather
  than information about it.
- `internal_flags` - `is_active`, `is_verified`, `is_superuser`,
  `onboarding_completed`, `settings_revision`, `Feedback.status`, the
  denormalized `reviewed_count`/`forwarded_count`/`dropped_count` on the user,
  and every `updated` column. System state and bookkeeping: a whole-row
  `onupdate` stamp moves when a counter is bumped and says nothing about the
  person, a triage label is our handling of a report rather than a fact about
  its author, and the counters are the `reviews` list below, counted.
- `transient_queue_state` - which posts are sitting in the review queue right
  now. Redis state that changes minute to minute, and the app is showing it to
  them as they read this.
- `post_vote_counts` - how many people forwarded or dropped each of their posts.
  Other people's actions in aggregate, and deliberately absent from every read
  route (see `PostRead` in CLAUDE.md), so an export is the wrong place to open
  that up.

Two smaller cuts follow from one principle - **do not describe a file you are
shipping**: media entries carry the archive path and the content type, not the
width, height, duration and byte size that the file itself answers. And internal
identifiers are dropped wherever a name does the job: `channel_id` (the name is
there), the Google `sub` (the address is there), Mollie's customer and
subscription ids (they identify the account at a processor, and a request to that
processor goes by email anyway). Post ids stay, because a post id is how a person
can point at one of their posts and be understood.

If a stricter reading is ever wanted, the answer is to add a field here - not to
go back to dumping rows.
"""

from datetime import datetime, timezone
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict


def _as_utc(value: datetime) -> datetime:
    """Stamp UTC on a naive timestamp rather than emitting an ambiguous one.

    Every timestamp in this schema comes from a `DateTime(timezone=True)` column,
    so a naive value can only mean a driver dropped the zone - never a genuinely
    local time. Pydantic serializes the result as ISO 8601 with an offset, which
    is what "machine-readable" means for a date.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


UtcDatetime = Annotated[datetime, AfterValidator(_as_utc)]


class _ExportModel(BaseModel):
    """Base for every section: nothing but the declared fields gets through.

    `extra="forbid"` is the point of the module expressed as configuration - a
    stray keyword at a construction site is an error here, not a new column in
    somebody's export.
    """

    model_config = ConfigDict(extra="forbid")


class ExportMediaFile(_ExportModel):
    """One uploaded file, and where to find it in the archive.

    "image" or "video" and a path, and nothing else - no byte size, no
    dimensions, no duration, not even the stored content type. All of those
    describe a file that is *in the same archive*, three lines below in the
    listing, and can be read off it exactly.
    """

    type: str
    #: Path inside the ZIP, so the JSON is navigable offline.
    file: str
    #: Videos only: the generated preview frame, its own object.
    poster_file: str | None = None


class ExportBlock(_ExportModel):
    """One block of a post - a paragraph of text, or an attachment."""

    position: int
    type: str
    text: str | None = None
    media: ExportMediaFile | None = None


class ExportPost(_ExportModel):
    """A post this account published.

    `is_probe` is absent because it could only ever be false: a test post is
    authored by the probe account, never by a reader (see app/core/probes.py).
    `subscription_kind` is absent because it is a rendering snapshot of the perk
    listed under `perks`.
    """

    id: int
    channel_name: str
    language: str
    is_anonymous: bool
    # Tokens other readers gifted this account for this post. Unlike the forward
    # and drop counts (`post_vote_counts`), the app shows the author this number,
    # and it is tokens they received - so it is theirs to take with them.
    gifted_tokens: int
    created: UtcDatetime
    blocks: list[ExportBlock]


class ExportReview(_ExportModel):
    """A verdict this account gave someone else's post.

    The post itself is not included - it belongs to whoever wrote it - so the id
    is what makes the record a record of something rather than a bare timestamp.
    """

    post_id: int
    verdict: str
    # Present (true) only on a forward whose earned token went to the author.
    gifted_token: bool | None = None
    created: UtcDatetime


class ExportProbeResponse(_ExportModel):
    """An attention check this account answered.

    `variant_code` is deliberately absent. It names the *template* a check was
    built from, which is a catalogue of what probes look like rather than a fact
    about the person - and handing that out is precisely the recognition problem
    the probe design is already careful about (see CLAUDE.md). What the check
    asked and what was answered is the part that concerns them, and it stays.
    """

    post_id: int
    expected_verdict: str
    given_verdict: str
    correct: bool
    created: UtcDatetime


class ExportIdentity(_ExportModel):
    """A linked sign-in provider.

    The provider's subject id (Google's `sub`) is absent: it is the opaque key
    this backend links on, it is worth nothing to carry anywhere else, and the
    address below is what actually identifies the connection to a person.
    """

    provider: str
    account_email: str | None = None


class ExportSubscription(_ExportModel):
    """A channel this account subscribed to."""

    channel_name: str
    subscribed_at: UtcDatetime


class ExportPerk(_ExportModel):
    """A subscription perk currently held (e.g. supporter)."""

    kind: str
    created: UtcDatetime


class ExportPayment(_ExportModel):
    """A supporter subscription, as this service records it.

    `processor` names the recipient of the payment data, which is the Art.
    15(1)(c) fact; the processor's own customer and subscription ids are not
    included, because they identify the account *there* and a request to them
    goes by email.
    """

    processor: str
    status: str
    current_period_end: UtcDatetime | None = None
    created: UtcDatetime


class ExportFeedback(_ExportModel):
    """A feedback submission still linked to this account.

    Anonymous submissions cannot appear: they store no `user_id` at all, which is
    the whole point of the option. `status` is absent - it is our internal triage
    of the report, not a fact about the person who sent it.
    """

    id: int
    kind: str
    message: str
    rating: int | None = None
    allow_contact: bool
    #: Server-stamped at submission: the consent that made storing it lawful.
    consented_at: UtcDatetime
    created: UtcDatetime
    media: list[ExportMediaFile] = []


class ExportItem(_ExportModel):
    """A row of the template's leftover `items` table.

    Nothing in the mobile app writes these, so in practice the list is empty -
    but the route still exists, so a row that does exist is personal data and
    goes in the file.
    """

    value: str | None = None
    created: UtcDatetime


class ExportAccount(_ExportModel):
    """The account itself: identity and the preferences its owner chose.

    Deliberately not here: `is_active`, `is_verified`, `is_superuser`,
    `onboarding_completed`, `settings_revision`, `updated`, and the three
    denormalized review counters - see the module docstring. `auth_provider` is
    gone too, because `identities` already answers it and a derived field that
    can disagree with its source is worse than no field.
    """

    id: str
    email: str
    username: str | None = None
    bio: str | None = None
    created: UtcDatetime
    #: Languages this reader accepts posts in - a preference they set.
    content_languages: list[str]
    dark_mode: bool
    profile_picture: ExportMediaFile | None = None


class ExportTokens(_ExportModel):
    """What this account can currently spend."""

    balance: int


class ExportReviewerTrust(_ExportModel):
    """The one piece of profiling this service does, and what it does.

    Art. 15(1)(h) asks for meaningful information about the logic and the
    envisaged consequences, which is `score`, the band it falls in, what that
    multiplies, how far back it looks, and a sentence of plain language. The raw
    fan-out constant it multiplies is an internal parameter and is not here.
    """

    score: int
    band: str
    reach_multiplier: float
    measured_over_days: int
    explanation: str


class ExportMeta(_ExportModel):
    """What this file is, and what it deliberately leaves out.

    `omitted` is a list of stable codes rather than sentences, for the same
    reason `api_error` hands the client a code: the words belong in the localized
    README, and a code can be matched against this module's docstring by whoever
    has to answer a question about it.
    """

    service: str
    format_version: int
    generated_at: UtcDatetime
    subject_id: str
    legal_basis: list[str]
    omitted: list[str]


class AccountExportDocument(_ExportModel):
    """`data.json` in full.

    Sections are always present, empty list included: "we hold none of these" is
    itself part of the answer Art. 15 asks for, and an absent key would read as
    an export that forgot to look.
    """

    export: ExportMeta
    account: ExportAccount
    identities: list[ExportIdentity]
    channel_subscriptions: list[ExportSubscription]
    posts: list[ExportPost]
    reviews: list[ExportReview]
    probe_responses: list[ExportProbeResponse]
    perks: list[ExportPerk]
    payments: list[ExportPayment]
    feedback: list[ExportFeedback]
    items: list[ExportItem]
    tokens: ExportTokens
    reviewer_trust: ExportReviewerTrust
