"""Pydantic request/response shapes for the Agent API.

Kept small on purpose: agents will rely on the auto-generated OpenAPI
spec at ``/api/v1/docs``, so every field needs a sensible description.

We deliberately don't expose internal fields like ``workspace_id`` in
request bodies — workspace scope comes from the bearer token, never
from client-supplied JSON. This is the same confused-deputy defence
Postiz uses.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import TYPE_CHECKING, Literal

from ninja import Field, Schema
from pydantic import field_serializer, field_validator

from providers import is_video_only, requires_media
from providers.youtube import rtmps_ingestion_url

if TYPE_CHECKING:
    from apps.analytics.derive import DerivedMetric
    from apps.composer.models import PlatformPost, Post


def _serialize_utc_z(value: dt.datetime | None) -> str | None:
    """Render a datetime as ISO 8601 with a trailing ``Z`` for UTC.

    Pydantic's default datetime serialization emits ``+00:00`` for UTC,
    while Django's JSON encoder emits ``Z``. Without explicit normalization
    REST and MCP responses can disagree for the same instant.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# /me
# ---------------------------------------------------------------------------


class AccountSummary(Schema):
    id: uuid.UUID
    platform: str
    transport: str | None = Field(
        None,
        description=(
            "Transport carrying publishes for this account: \"zernio\" when routed via Zernio, "
            "null for a direct platform connection. ``platform`` is always the effective "
            "destination platform (TikTok, YouTube, ...) — never the transport."
        ),
    )
    account_name: str
    account_handle: str = ""
    connection_status: str
    char_limit: int = Field(
        2200,
        description="Maximum caption length the platform accepts. Reject locally before calling /posts.",
    )
    escaped_chars: str = Field(
        "",
        description=(
            "Characters the platform requires escaped, each costing two against ``char_limit``. "
            "LinkedIn escapes its reserved little-text set, so count a caption as "
            "``len(caption) + sum(caption.count(c) for c in escaped_chars)`` before rejecting it. "
            "Empty for platforms that publish the caption verbatim."
        ),
    )
    needs_title: bool = Field(
        False,
        description="True when the platform requires a title (YouTube, Pinterest). When false, the title field is ignored.",
    )
    supports_first_comment: bool = Field(
        True,
        description=(
            "True when the platform can auto-post a first comment after publish. "
            "False for TikTok, Pinterest, Bluesky, Google Business. "
            "LinkedIn Personal returns false when authorized via OIDC instead of the Community Management API. "
            "If false, ``first_comment`` is silently dropped at publish time."
        ),
    )
    requires_video: bool = Field(
        False,
        description=(
            "True when the platform publishes only video (TikTok, YouTube). Scheduling a post "
            "with no video in ``media_asset_ids`` is rejected with 422."
        ),
    )
    requires_media: bool = Field(
        False,
        description=(
            "True when the platform can't publish text alone (Instagram, Pinterest, and the "
            "``requires_video`` platforms). Scheduling a post with no ``media_asset_ids`` is "
            "rejected with 422."
        ),
    )

    @classmethod
    def from_social_account(cls, sa) -> AccountSummary:
        return cls(
            id=sa.id,
            platform=sa.display_platform,
            transport=sa.transport,
            account_name=sa.account_name,
            account_handle=getattr(sa, "account_handle", "") or "",
            connection_status=sa.connection_status,
            char_limit=sa.char_limit,
            escaped_chars=sa.escaped_chars,
            needs_title=bool(sa.field_config.get("needs_title", False)),
            supports_first_comment=sa.supports_first_comment(),
            requires_video=is_video_only(sa.display_platform),
            requires_media=requires_media(sa.display_platform),
        )


class StorageSummary(Schema):
    used_bytes: int
    limit_bytes: int
    remaining_bytes: int
    plan_slug: str = Field("", description="IntelligenceSubscription plan slug; empty if no active subscription.")


class MeResponse(Schema):
    """Echoes everything the key is scoped to so an agent can self-introspect."""

    api_key_id: uuid.UUID
    workspace_id: uuid.UUID
    workspace_name: str
    organization_id: uuid.UUID
    permissions: list[str]
    storage: StorageSummary
    allowlisted_accounts: list[AccountSummary]


# ---------------------------------------------------------------------------
# /accounts
# ---------------------------------------------------------------------------


class AccountsListResponse(Schema):
    accounts: list[AccountSummary]
    limit: int
    next_cursor: str | None = None


class AccountProfileUpdateRequest(Schema):
    """Body for ``PATCH /accounts/{id}/profile``.

    Which fields apply depends on the account's destination platform
    (``GET /accounts/``): YouTube takes ``description`` only; a
    Facebook Page takes ``about`` / ``description`` / ``website`` /
    ``phone``. TikTok and Instagram take none — their bio can only be
    changed in the app. Send at least one field; blank strings count
    as omitted, never as "clear this field".
    """

    description: str | None = Field(
        None,
        max_length=5000,
        description=(
            "YouTube channel description (also the Page long description for Facebook). "
            "YouTube enforces max 1000 characters per platform; Facebook accepts up to 5000 here."
        ),
    )
    about: str | None = Field(
        None,
        max_length=255,
        description="Facebook Page short About text. Facebook only.",
    )
    website: str | None = Field(
        None,
        max_length=2000,
        description="Facebook Page website. Must be an http(s) URL. Facebook only.",
    )
    phone: str | None = Field(None, max_length=50, description="Facebook Page phone. Facebook only.")


class AccountProfileResponse(Schema):
    """What ``PATCH /accounts/{id}/profile`` changed on the platform."""

    id: uuid.UUID
    platform: str
    transport: str | None = None
    description: str | None = None
    about: str | None = None
    website: str | None = None
    phone: str | None = None
    updated_fields: list[str] = Field(
        default_factory=list,
        description="Fields the provider confirmed as updated.",
    )


# ---------------------------------------------------------------------------
# /posts — write
# ---------------------------------------------------------------------------


PostAction = Literal["draft", "schedule"]


class PlatformOverride(Schema):
    """Per-account override of the post's title/caption/first_comment.

    Mirrors the three form fields the cookie-authenticated UI accepts
    (see [apps/composer/views.py:85-96]). Maps to the model's
    ``platform_specific_*`` columns on ``PlatformPost``.

    Each field is independent: omit a field (or send ``null``) to keep
    the post's default for that platform. Send a string (including ``""``
    to explicitly blank out the value just for this platform) to apply
    the override.
    """

    social_account_id: uuid.UUID = Field(
        ...,
        description="Must reference an account that is a child of this post.",
    )
    title: str | None = Field(None, max_length=255)
    caption: str | None = Field(None, max_length=10_000)
    first_comment: str | None = Field(None, max_length=10_000)


def _clean_youtube_tags(value: list[str] | None) -> list[str] | None:
    """Enforce YouTube's tag caps (same bounds as ``apps/common/validators.py``).

    At most 30 tags, each 1-50 chars after trimming, 500 chars total
    including separators. Blank entries are rejected; duplicates are
    dropped, order kept. Violations raise ``ValueError`` → 422.
    Shared by create and PATCH so the two surfaces accept exactly the
    same tags.
    """
    if value is None:
        return None
    if len(value) > 30:
        raise ValueError("tags accepts at most 30 entries.")
    cleaned: list[str] = []
    seen: set[str] = set()
    total = 0
    for raw in value:
        tag = raw.strip()
        if not tag:
            raise ValueError("tags must not contain blank entries.")
        if len(tag) > 50:
            raise ValueError("each tag must be at most 50 characters.")
        if tag in seen:
            continue
        total += len(tag) + (1 if cleaned else 0)
        if total > 500:
            raise ValueError("tags must total at most 500 characters including separators.")
        seen.add(tag)
        cleaned.append(tag)
    return cleaned


class CreatePostRequest(Schema):
    """Create a draft or directly schedule a post against one account.

    ``social_account_id`` MUST be in the key's allowlist; the auth class
    raises 403 otherwise.

    The YouTube-only fields (``privacy_status``, ``tags``,
    ``thumbnail_asset_id``, ``category_id``, ``made_for_kids``) are
    honoured only when the target account's destination platform is
    YouTube; every other destination ignores them. All five are
    optional — omitting them keeps the legacy behaviour (public, no
    tags, no custom thumbnail, category ``"22"``, not made for kids).
    """

    social_account_id: uuid.UUID = Field(
        ...,
        description="ID of the SocialAccount to target. Must be in the key's allowlist.",
    )
    caption: str = Field(..., max_length=10_000)
    title: str = Field(
        "",
        max_length=255,
        description="Required on platforms where ``needs_title=true`` (YouTube, Pinterest). Ignored otherwise.",
    )
    first_comment: str = Field(
        "",
        max_length=10_000,
        description=(
            "Auto-posted as a reply ~120s after the main post lands. "
            "Silently dropped at publish time when the target account's "
            "``supports_first_comment`` is false (TikTok, Pinterest, "
            "Bluesky, Google Business; LinkedIn Personal in OIDC mode)."
        ),
    )
    internal_notes: str = Field(
        "",
        max_length=10_000,
        description=(
            "Private team-only note. Never published to any platform; surfaced only "
            "to your team via the API and the composer."
        ),
    )
    media_asset_ids: list[uuid.UUID] = Field(
        default_factory=list,
        description="MediaAsset IDs already uploaded to the workspace's media library. Position-ordered.",
    )
    # --- YouTube-only options (ignored for every other destination) ---
    privacy_status: Literal["public", "unlisted", "private"] | None = Field(
        None,
        description=(
            "YouTube privacy_status. Omitted → the provider default (public). "
            "Via Zernio, 'unlisted' is delivered as private — Zernio has no unlisted visibility."
        ),
    )
    tags: list[str] | None = Field(
        None,
        description=(
            "YouTube video tags. At most 30 tags, each 1-50 characters after trimming, "
            "500 characters total including separators (the YouTube Data API caps). "
            "Duplicates are dropped, order kept. Omitted → no tags."
        ),
    )
    thumbnail_asset_id: uuid.UUID | None = Field(
        None,
        description=(
            "MediaAsset ID (image) in this workspace to upload as the YouTube custom thumbnail. "
            "Must exist in the same workspace — otherwise 422. Ignored via Zernio "
            "(Zernio has no thumbnail field). A failed thumbnail upload never fails "
            "the video publish (provider behaviour)."
        ),
    )
    category_id: str | None = Field(
        None,
        max_length=10,
        pattern=r"^[0-9]+$",
        description=(
            'YouTube categoryId (numeric string, e.g. "22" = People & Blogs). '
            'Omitted → provider default "22". Ignored via Zernio.'
        ),
    )
    made_for_kids: bool | None = Field(
        None,
        description="YouTube selfDeclaredMadeForKids. Omitted → false. Ignored via Zernio.",
    )

    @field_validator("tags")
    @classmethod
    def _validate_youtube_tags(cls, value: list[str] | None) -> list[str] | None:
        return _clean_youtube_tags(value)
    platform_overrides: list[PlatformOverride] = Field(
        default_factory=list,
        description=(
            "Optional per-platform overrides of title / caption / first_comment. "
            "Each entry's social_account_id must equal the post's social_account_id "
            "in the current single-account API. Sending a field as ``null`` (or "
            "omitting it) leaves the post's default in place for that platform."
        ),
    )
    action: PostAction = Field(
        "draft",
        description=(
            "``draft`` parks the post for later editing/scheduling; "
            "``schedule`` requires ``scheduled_at`` and queues for publishing."
        ),
    )
    scheduled_at: dt.datetime | None = Field(
        None,
        description="UTC timestamp. Required when ``action='schedule'``.",
    )
    proposed_publish_at: dt.datetime | None = Field(
        None,
        description=(
            "Optional draft-stage suggested publish time (UTC), independent of "
            "``scheduled_at``. Stored as-is — not validated against the future and "
            "never queued for publishing; cleared automatically if the post is scheduled."
        ),
    )
    idempotency_key: str | None = Field(
        None,
        max_length=128,
        description=(
            "Optional client-chosen retry key. When both this field and the "
            "`Idempotency-Key` header are sent, this field wins. Same key + same "
            "body → the first response is replayed verbatim; same key + different "
            "body → 422; a concurrent request holding the same key → 409. "
            "Keys live 24h and are scoped to this API key."
        ),
    )


class UpdatePostRequest(Schema):
    """Body for ``PATCH /posts/{id}``.

    Every field is optional; ``null`` (or omitted) leaves the field
    unchanged — the one exception is ``tags``, where an empty list ``[]``
    clears the stored tags. Only posts whose aggregate status is editable
    (draft / scheduled / review states — never published) accept a PATCH;
    anything else answers 409.

    The YouTube-only fields (``privacy_status``, ``tags``,
    ``thumbnail_asset_id``, ``category_id``, ``made_for_kids``) apply to
    the post's YouTube-destination platform children only, and only while
    those children are still editable (draft / scheduled). The publisher
    reads them from the child at publish time, so a PATCH before publish
    takes effect. Sending any of them when the post has no YouTube child
    answers 422, and so does a ``thumbnail_asset_id`` that is not a media
    asset in this workspace. On a YouTube child routed via Zernio,
    ``category_id`` / ``made_for_kids`` / ``thumbnail_asset_id`` answer
    422 (Zernio has no such fields — only ``privacy_status`` and ``tags``
    survive there); the other two fields keep the create-time contract.
    There is no way to clear ``thumbnail_asset_id`` / ``category_id`` via
    PATCH (``null`` is a no-op everywhere here); send the provider default
    ``category_id`` ``"22"`` to neutralise a custom category.
    """

    caption: str | None = Field(None, max_length=10_000)
    title: str | None = Field(None, max_length=255)
    first_comment: str | None = Field(None, max_length=10_000)
    internal_notes: str | None = Field(
        None,
        max_length=10_000,
        description="Private team-only note. Omit (or send null) to leave it unchanged.",
    )
    media_asset_ids: list[uuid.UUID] | None = None
    scheduled_at: dt.datetime | None = Field(
        None,
        description="If the post is currently scheduled, this re-times it. Ignored for drafts.",
    )
    proposed_publish_at: dt.datetime | None = Field(
        None,
        description="Set a draft's suggested publish time (UTC). Sending null is a no-op (cannot clear via PATCH).",
    )
    # --- YouTube-only options (same bounds as CreatePostRequest) ---
    privacy_status: Literal["public", "unlisted", "private"] | None = Field(
        None,
        description="YouTube privacy_status for the post's YouTube children. Omitted → unchanged.",
    )
    tags: list[str] | None = Field(
        None,
        description=(
            "YouTube video tags (same caps as create: at most 30, each 1-50 chars, "
            "500 chars total). An empty list clears the stored tags."
        ),
    )
    thumbnail_asset_id: uuid.UUID | None = Field(
        None,
        description=(
            "MediaAsset ID (image) in this workspace to upload as the YouTube custom "
            "thumbnail. Must exist in the same workspace — otherwise 422. "
            "Not available via Zernio (422). Cannot be cleared via PATCH."
        ),
    )
    category_id: str | None = Field(
        None,
        max_length=10,
        pattern=r"^[0-9]+$",
        description=(
            'YouTube categoryId (numeric string, e.g. "22" = People & Blogs). '
            "Not available via Zernio (422)."
        ),
    )
    made_for_kids: bool | None = Field(
        None,
        description="YouTube selfDeclaredMadeForKids. Not available via Zernio (422).",
    )

    @field_validator("tags")
    @classmethod
    def _validate_youtube_tags(cls, value: list[str] | None) -> list[str] | None:
        return _clean_youtube_tags(value)


class ScheduleRequest(Schema):
    scheduled_at: dt.datetime = Field(..., description="UTC timestamp at which the publisher should fire the post.")


# ---------------------------------------------------------------------------
# /posts — read
# ---------------------------------------------------------------------------


class PlatformPostSummary(Schema):
    id: uuid.UUID
    social_account_id: uuid.UUID
    platform: str
    transport: str | None = None
    status: str
    scheduled_at: dt.datetime | None
    published_at: dt.datetime | None
    platform_post_id: str = ""
    publish_error: str = ""
    # YouTube-only options as stored on this child (``platform_extra``).
    # ``None`` / empty means "not set — the provider default applies at
    # publish time" (public, no tags, no thumbnail, category "22", not made
    # for kids). Always ``None`` / empty for non-YouTube children. Set at
    # create time and editable via ``PATCH /posts/{id}`` while the child is
    # still draft / scheduled.
    youtube_privacy_status: str | None = None
    youtube_tags: list[str] = Field(default_factory=list)
    youtube_thumbnail_asset_id: uuid.UUID | None = None
    youtube_category_id: str | None = None
    youtube_made_for_kids: bool | None = None

    @field_serializer("scheduled_at", "published_at")
    def _serialize_dt(self, value: dt.datetime | None) -> str | None:
        return _serialize_utc_z(value)

    @classmethod
    def from_platform_post(cls, pp: PlatformPost) -> PlatformPostSummary:
        extra = pp.platform_extra or {}
        thumb_raw = extra.get("thumbnail_asset_id")
        try:
            thumb_id = uuid.UUID(str(thumb_raw)) if thumb_raw else None
        except (ValueError, TypeError, AttributeError):
            thumb_id = None
        return cls(
            id=pp.id,
            social_account_id=pp.social_account_id,
            platform=pp.social_account.display_platform,
            transport=pp.social_account.transport,
            status=pp.status,
            scheduled_at=pp.scheduled_at,
            published_at=pp.published_at,
            platform_post_id=pp.platform_post_id or "",
            publish_error=pp.publish_error or "",
            youtube_privacy_status=extra.get("privacy_status"),
            youtube_tags=list(extra.get("tags") or []),
            youtube_thumbnail_asset_id=thumb_id,
            youtube_category_id=extra.get("category_id"),
            youtube_made_for_kids=extra.get("self_declared_made_for_kids"),
        )


class PostResponse(Schema):
    id: uuid.UUID
    workspace_id: uuid.UUID
    title: str
    caption: str
    first_comment: str
    internal_notes: str
    scheduled_at: dt.datetime | None
    published_at: dt.datetime | None
    proposed_publish_at: dt.datetime | None
    status: str  # derived aggregate
    platform_posts: list[PlatformPostSummary]
    created_at: dt.datetime
    updated_at: dt.datetime

    @field_serializer("scheduled_at", "published_at", "proposed_publish_at", "created_at", "updated_at")
    def _serialize_dt(self, value: dt.datetime | None) -> str | None:
        return _serialize_utc_z(value)

    @classmethod
    def from_post(cls, post: Post, *, include_internal_notes: bool = False) -> PostResponse:
        """Single source of truth for Post → API response shape.

        Used by both the REST router and MCP handlers so the two surfaces
        cannot drift. ``post.platform_posts`` should be prefetched with
        ``social_account`` to avoid an N+1.

        ``internal_notes`` is a team-only field. The composer hides it from
        ``client`` / ``viewer`` workspace roles, so the API/MCP mirror that by
        redacting it to ``""`` unless ``include_internal_notes`` is set. Call
        sites derive that flag from the caller's ``create_posts`` permission
        (see ``_can_view_internal_notes`` in the REST router and MCP handlers).
        It defaults to ``False`` so a new, unconverted call site fails closed —
        redacted — rather than leaking notes.
        """
        # ``.select_related()`` builds a *new* queryset, which discards any
        # prefetch cache and re-queries once per post — an N+1 on list views.
        # Use the cache when the caller prefetched, and fall back otherwise so
        # an un-prefetched single fetch still avoids a query per child.
        if "platform_posts" in getattr(post, "_prefetched_objects_cache", {}):
            children = post.platform_posts.all()
        else:
            children = post.platform_posts.select_related("social_account")
        platform_posts = [PlatformPostSummary.from_platform_post(pp) for pp in children]
        return cls(
            id=post.id,
            workspace_id=post.workspace_id,
            title=post.title,
            caption=post.caption,
            first_comment=post.first_comment,
            internal_notes=post.internal_notes if include_internal_notes else "",
            scheduled_at=post.scheduled_at,
            published_at=post.published_at,
            proposed_publish_at=post.proposed_publish_at,
            status=post.status,
            platform_posts=platform_posts,
            created_at=post.created_at,
            updated_at=post.updated_at,
        )


class PostsListResponse(Schema):
    """``GET /posts/`` — workspace posts visible to this key, newest first.

    A post is listed only when every one of its platform children targets
    an account in the key's allowlist (same rule as ``GET /posts/{id}``);
    posts with no children are never listed. ``status`` matches on the
    per-platform child status (identical to the aggregate status for the
    single-child posts the API creates).
    """

    posts: list[PostResponse]
    limit: int
    next_cursor: str | None = None


# ---------------------------------------------------------------------------
# /media — write + read
# ---------------------------------------------------------------------------


class MediaAssetResponse(Schema):
    """Single MediaAsset as exposed to the API.

    ``url`` is whatever ``MediaAsset.file.url`` resolves to — a presigned
    URL with a configurable TTL on S3/R2, or a local ``/media/...`` path
    on local storage. Agents should treat it as short-lived and always
    reference the asset by ``id`` in subsequent calls.

    ``last_used_at`` is the max ``created_at`` across every Post that
    references this asset via ``PostMedia``. It is populated only on
    list/detail responses, never on the upload 201.
    """

    id: uuid.UUID
    # ``organization`` is a nullable FK on ``MediaAsset`` (see
    # [apps/media_library/models.py:93-99]). Pre-migration legacy rows can
    # exist with org=NULL, and Pydantic rejects them at response time if
    # this isn't optional. Staging hit this on 2026-06-01 with the agent
    # API media list endpoint.
    organization_id: uuid.UUID | None
    workspace_id: uuid.UUID | None
    filename: str
    media_type: str
    mime_type: str
    file_size: int
    file_size_display: str
    width: int
    height: int
    aspect_ratio: float
    duration: float
    title: str
    alt_text: str
    tags: list[str]
    folder_id: uuid.UUID | None
    is_starred: bool
    is_shared: bool
    processing_status: str
    url: str
    thumbnail_url: str | None
    last_used_at: dt.datetime | None = None
    created_at: dt.datetime
    updated_at: dt.datetime

    @field_serializer("last_used_at", "created_at", "updated_at")
    def _serialize_dt(self, value: dt.datetime | None) -> str | None:
        return _serialize_utc_z(value)

    @classmethod
    def from_asset(cls, asset, *, last_used_at: dt.datetime | None = None) -> MediaAssetResponse:
        return cls(
            id=asset.id,
            organization_id=asset.organization_id,
            workspace_id=asset.workspace_id,
            filename=asset.filename,
            media_type=asset.media_type,
            mime_type=asset.mime_type or "",
            file_size=int(asset.file_size or 0),
            file_size_display=asset.file_size_display,
            width=int(asset.width or 0),
            height=int(asset.height or 0),
            aspect_ratio=float(asset.aspect_ratio or 0),
            duration=float(asset.duration or 0),
            title=asset.title or "",
            alt_text=asset.alt_text or "",
            tags=list(asset.tags or []),
            folder_id=asset.folder_id,
            is_starred=bool(asset.is_starred),
            is_shared=bool(asset.is_shared),
            processing_status=asset.processing_status,
            url=_safe_file_url(asset.file),
            thumbnail_url=_safe_file_url(asset.thumbnail) or None,
            last_used_at=last_used_at,
            created_at=asset.created_at,
            updated_at=asset.updated_at,
        )


def _safe_file_url(file_field) -> str:
    """Return ``file_field.url`` if the field is set, else ``""``.

    ``FileField.url`` raises ``ValueError`` when the field is blank, which
    happens for ``thumbnail`` on every asset before async processing
    finishes.
    """
    if not file_field:
        return ""
    try:
        return file_field.url or ""
    except (ValueError, AttributeError):
        return ""


class MediaAssetListResponse(Schema):
    items: list[MediaAssetResponse]
    next_cursor: str | None = None
    limit: int


# ---------------------------------------------------------------------------
# /media/uploads — chunked/resumable uploads (v1.2.0)
# ---------------------------------------------------------------------------


class UploadSessionRequest(Schema):
    """``POST /media/uploads/`` — start a chunked upload session."""

    filename: str = Field(..., min_length=1, description="Original filename, e.g. ``clip.mp4``.")
    total_bytes: int = Field(..., gt=0, description="Declared total size in bytes (must fit the media_type cap).")
    media_type: str = Field(
        ...,
        description="``image`` | ``video`` | ``gif`` | ``document`` — picks the size cap.",
    )
    content_type: str | None = Field(
        None,
        description="Declared MIME; must be in the allowlist or ``application/octet-stream``.",
    )
    alt_text: str = ""
    title: str = ""
    tags: list[str] = Field(default_factory=list)
    folder_id: uuid.UUID | None = None
    idempotency_key: str | None = Field(
        None,
        description=(
            "Optional client-chosen retry key. Wins over the `Idempotency-Key` "
            "header when both are sent. Same key + same upload → the current "
            "session state is replayed; same key + different upload → 422; "
            "concurrent holder → 409."
        ),
    )


class UploadSessionResponse(Schema):
    """A chunked upload session — returned by init, status, and replay."""

    upload_id: uuid.UUID
    upload_mode: str = Field(..., description="``s3_multipart`` (bytes go straight to storage) or ``server_append`` (bytes PUT to Bean).")
    filename: str
    total_bytes: int
    chunk_size: int = Field(..., description="Bytes per part (server-chosen, 8 MiB).")
    total_parts: int
    uploaded_bytes: int
    received_parts: list[int] = Field(default_factory=list)
    expires_at: dt.datetime
    next_part: int | None = Field(None, description="First missing part number, or null when every part arrived.")
    media_asset_id: uuid.UUID | None = Field(None, description="Set once the session is finalized.")

    @field_serializer("expires_at")
    def _serialize_expires_at(self, value: dt.datetime | None) -> str | None:
        return _serialize_utc_z(value)


class UploadPartRequest(Schema):
    """``POST /media/uploads/{upload_id}/parts`` — ask for one part's PUT URL."""

    part_number: int = Field(..., ge=1, description="1-based part number, up to total_parts.")


class UploadPartResponse(Schema):
    """Presigned (S3) or Bean-local (server-append) PUT URL for one part."""

    upload_id: uuid.UUID
    part_number: int
    method: str = "PUT"
    url: str
    expires_at: dt.datetime

    @field_serializer("expires_at")
    def _serialize_expires_at(self, value: dt.datetime | None) -> str | None:
        return _serialize_utc_z(value)


class UploadChunkResponse(Schema):
    """``PUT /media/uploads/{upload_id}/parts/{part_number}`` — one local part stored."""

    upload_id: uuid.UUID
    part_number: int
    uploaded_bytes: int
    next_part: int | None = Field(None, description="Next missing part number, or null when every part arrived.")


class UploadSessionAbortResponse(Schema):
    upload_id: uuid.UUID
    deleted: bool = True


class UploadSessionCompleteRequest(Schema):
    """``POST /media/uploads/{upload_id}/complete`` — optional asset metadata.

    The metadata supplied at init is validated there (folder existence,
    media type) but applied here, at finalize time — mirroring the MCP
    ``finalize_media_upload`` tool, which takes folder/tags at finalize.
    """

    alt_text: str = ""
    title: str = ""
    tags: list[str] = Field(default_factory=list)
    folder_id: uuid.UUID | None = None


class PostDeleteResponse(Schema):
    """``DELETE /posts/{id}`` — mirrors ``LiveDeleteResponse`` (Agent B)."""

    id: uuid.UUID
    deleted: bool = True


# ---------------------------------------------------------------------------
# /analytics — read
# ---------------------------------------------------------------------------


class DerivedMetricResponse(Schema):
    """A single derived metric over a rolling window.

    Mirrors ``apps.analytics.derive.DerivedMetric`` but with a stable wire
    shape: agents can rely on every metric carrying the same fields.
    The dataclass itself isn't a Pydantic model, so we route through
    :meth:`from_derived` rather than letting Ninja convert it implicitly.
    """

    key: str = Field(..., description="Metric identifier (e.g. ``views``, ``likes``, ``engagement``).")
    label: str = Field(..., description="Human-readable label for the metric (e.g. ``Views``).")
    kind: str = Field(..., description="Format hint: ``count`` | ``percent`` | ``minutes``.")
    value: float = Field(
        ...,
        description=(
            "Aggregate value over the current window. Sum for ``count`` metrics, "
            "YouTube uses the native whole-period report for every metric, including watch-time totals "
            "and averageViewPercentage. Other platforms may average daily percent/minute values."
        ),
    )
    delta: float = Field(
        ...,
        description=(
            "Percent change vs. the previous equal-length window, calculated by BrightBean. "
            "``0.0`` when there is no prior baseline."
        ),
    )
    series: list[float | None] = Field(
        default_factory=list,
        description=(
            "Daily values for the current window, oldest first. ``null`` for a day the platform "
            "hasn't reported yet (for example YouTube Analytics' 2-3 day lag), which is not a zero."
        ),
    )
    calculated_by_brightbean: bool = Field(
        False,
        description=(
            "``true`` when BrightBean calculated ``value`` rather than the platform reporting it: the "
            "engagement rate, daily averages, and estimates on supported platforms. "
            "Disclosure does not permit prohibited derived metrics. YouTube values are native reports."
        ),
    )
    averaged: bool = Field(False, description="``value`` is BrightBean's average of the platform's daily figures.")
    estimated: bool = Field(
        False,
        description=(
            "``value`` was estimated by BrightBean from other counts — summed per-post deltas when the "
            "platform's account-level analytics weren't available or were older, or differences between "
            "follower totals."
        ),
    )
    comparison_available: bool = Field(True, description="Whether delta has a complete, nonzero comparison baseline.")

    @classmethod
    def from_derived(
        cls, key: str, label: str, derived: DerivedMetric, *, calculated: bool = False
    ) -> DerivedMetricResponse:
        """``calculated`` marks a value BrightBean always computes (the
        engagement rate), on top of what ``derived`` records about itself."""
        return cls(
            key=key,
            label=label,
            kind=derived.kind,
            value=derived.value,
            delta=derived.delta,
            series=list(derived.series),
            calculated_by_brightbean=calculated or derived.averaged or derived.estimated,
            averaged=derived.averaged,
            estimated=derived.estimated,
            comparison_available=derived.comparison_available,
        )


class EngagementCardResponse(Schema):
    rate: DerivedMetricResponse = Field(
        ...,
        description=(
            "Engagement rate = sum(parts) / denom * 100, calculated by BrightBean. The denom is the "
            "first of ``views, reach, impressions, plays`` with data, falling back to the follower count."
        ),
    )
    parts: list[DerivedMetricResponse] = Field(
        default_factory=list,
        description="The component metrics that feed the rate's numerator (likes, comments, shares, …).",
    )
    formula: str | None = Field(
        None,
        description=(
            "How the rate was calculated, e.g. ``(Likes + Comments + Shares) ÷ Views``. "
            "``null`` when there was nothing to divide by yet."
        ),
    )


class ReportingPeriodResponse(Schema):
    start: dt.date
    end: dt.date
    timezone: str
    latest_reported_day: dt.date | None = None


class AccountAnalyticsResponse(Schema):
    """Channel-level analytics summary over a rolling window of days."""

    account_id: uuid.UUID
    platform: str
    transport: str | None = Field(
        None,
        description=(
            "Transport carrying publishes for this account: \"zernio\" when routed via Zernio, "
            "null for a direct platform connection. ``platform`` is always the effective "
            "destination platform (TikTok, YouTube, ...) — never the transport."
        ),
    )
    account_name: str
    connection_status: str = Field(
        ...,
        description=(
            "``connected``, ``token_expiring``, ``disconnected``, or ``error``. "
            "Disconnected accounts still return historical analytics, but new data may not be flowing."
        ),
    )
    days: int = Field(..., description="Window size (in days) used for derivation. One of 7, 30, or 90.")
    reporting_period: ReportingPeriodResponse | None = None
    analytics_available: bool = Field(
        ...,
        description=(
            "``false`` for platforms whose APIs don't expose aggregate analytics "
            "(currently LinkedIn Personal, Bluesky, Mastodon). Metric arrays are empty when false."
        ),
    )
    unavailable_reason: str | None = Field(
        None,
        description="Human-readable reason the analytics surface is unavailable, when ``analytics_available`` is false.",
    )
    hero_metrics: list[DerivedMetricResponse] = Field(
        default_factory=list,
        description="Top-line per-channel KPIs. YouTube includes separate likes, comments and shares here.",
    )
    engagement: EngagementCardResponse | None = Field(
        None,
        description="Engagement-rate card, or ``null`` when the platform lacks a reach-like denominator.",
    )
    follower_growth: DerivedMetricResponse | None = Field(
        None,
        description="New followers / subscribers gained over the window, when the platform reports it.",
    )
    captured_at: dt.datetime | None = Field(
        None,
        description="Most recent ``captured_at`` across the account's snapshots. ``null`` if no rows yet.",
    )
    next_sync_eta: dt.datetime | None = Field(
        None,
        description=(
            "Approximate next time the background sync will refresh this account. "
            "``null`` for platforms without analytics. Use it to pick a polling delay."
        ),
    )

    @field_serializer("captured_at", "next_sync_eta")
    def _serialize_dt(self, value: dt.datetime | None) -> str | None:
        return _serialize_utc_z(value)


class PostMetricTileResponse(Schema):
    """One metric tile in the per-post analytics drawer."""

    key: str
    label: str
    kind: str
    value: float = Field(..., description="Most recent snapshot value for this metric on this post.")
    series: list[float] = Field(
        default_factory=list,
        description="Daily sparkline since publish — one entry per day with a recorded snapshot.",
    )
    is_primary: bool = Field(
        False,
        description="True for the platform's default headline metric (e.g. ``views`` for YouTube).",
    )


class PlatformPostAnalyticsResponse(Schema):
    """Analytics for a single per-platform child of a Post."""

    platform_post_id: uuid.UUID
    social_account_id: uuid.UUID
    platform: str
    transport: str | None = Field(
        None,
        description=(
            "Transport carrying publishes for this account: \"zernio\" when routed via Zernio, "
            "null for a direct platform connection. ``platform`` is always the effective "
            "destination platform (TikTok, YouTube, ...) — never the transport."
        ),
    )
    status: str
    published_at: dt.datetime | None
    analytics_available: bool = Field(
        ...,
        description=(
            "``false`` for platforms without analytics. ``true`` for everything else — including "
            "drafts and scheduled posts, which simply return empty metric tiles until publish."
        ),
    )
    unavailable_reason: str | None = None
    metric_tiles: list[PostMetricTileResponse] = Field(
        default_factory=list,
        description="One tile per metric this platform reports. Empty until the first snapshot lands.",
    )
    captured_at: dt.datetime | None = Field(
        None,
        description="Most recent ``captured_at`` across this platform-post's snapshots.",
    )
    next_sync_eta: dt.datetime | None = Field(
        None,
        description=(
            "Approximate next sync time based on the post's age: <24h→+1h, 1-7d→+6h, 7-30d→+1d, "
            "30-90d→+7d, >90d→null (syncs have stopped)."
        ),
    )

    @field_serializer("published_at", "captured_at", "next_sync_eta")
    def _serialize_dt(self, value: dt.datetime | None) -> str | None:
        return _serialize_utc_z(value)


class PostAnalyticsResponse(Schema):
    """Analytics for a Post, with per-platform breakdown.

    Mirrors ``PostResponse``'s parent-Post-with-platform-children shape so
    agents that already track ``post_id`` from ``schedule_post`` /
    ``create_draft`` can poll analytics without learning new IDs.
    """

    post_id: uuid.UUID
    workspace_id: uuid.UUID
    title: str
    caption: str
    platform_posts: list[PlatformPostAnalyticsResponse]


# ---------------------------------------------------------------------------
# /inbox — read + reply drafting
# ---------------------------------------------------------------------------


class InboxReplyResponse(Schema):
    """One outbound reply to an inbox message.

    A reply is created as ``draft``, then a send step delivers it to the
    platform and moves it to ``sent`` (or ``failed`` with a human-readable
    ``send_error`` if the platform refused it). ``sent_at`` /
    ``platform_reply_id`` are populated only once ``status == "sent"``.
    """

    id: uuid.UUID
    inbox_message_id: uuid.UUID
    status: str
    body: str
    author_email: str = ""
    platform_reply_id: str = ""
    send_error: str = ""
    created_at: dt.datetime
    updated_at: dt.datetime
    sent_at: dt.datetime | None = None

    @field_serializer("created_at", "updated_at", "sent_at")
    def _serialize_dt(self, value: dt.datetime | None) -> str | None:
        return _serialize_utc_z(value)

    @classmethod
    def from_reply(cls, reply) -> InboxReplyResponse:
        author = getattr(reply, "author", None)
        return cls(
            id=reply.id,
            inbox_message_id=reply.inbox_message_id,
            status=reply.status,
            body=reply.body,
            author_email=(getattr(author, "email", "") or ""),
            platform_reply_id=reply.platform_reply_id or "",
            send_error=reply.send_error or "",
            created_at=reply.created_at,
            updated_at=reply.updated_at,
            sent_at=reply.sent_at,
        )


class InboxAttachmentResponse(Schema):
    type: str
    url: str = ""


class InboxReplyEligibilityResponse(Schema):
    can_send: bool
    reason: str = ""
    window: str
    standard_reply_until: dt.datetime | None = None
    human_reply_until: dt.datetime | None = None
    human_agent: bool = False

    @field_serializer("standard_reply_until", "human_reply_until")
    def _serialize_deadline(self, value):
        return _serialize_utc_z(value)


class InboxMessageResponse(Schema):
    """An inbound comment / mention / DM / review in the unified inbox."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    social_account_id: uuid.UUID
    platform: str
    transport: str | None = Field(
        None,
        description=(
            "Transport carrying replies for this account: \"zernio\" when routed via Zernio, "
            "null for a direct platform connection. ``platform`` is always the effective "
            "destination platform -- never the transport."
        ),
    )
    message_type: str
    status: str
    sentiment: str
    sender_name: str
    sender_handle: str = ""
    body: str
    related_post_id: uuid.UUID | None = None
    received_at: dt.datetime
    created_at: dt.datetime
    replies: list[InboxReplyResponse] = Field(default_factory=list)
    attachments: list[InboxAttachmentResponse] = Field(default_factory=list)
    reply_eligibility: InboxReplyEligibilityResponse = Field(
        description="Platform and reply-window eligibility for REST/MCP. Sending also requires reply_from_inbox permission."
    )

    @field_serializer("received_at", "created_at")
    def _serialize_dt(self, value: dt.datetime | None) -> str | None:
        return _serialize_utc_z(value)

    @classmethod
    def from_message(cls, message, *, include_replies: bool = False) -> InboxMessageResponse:
        from apps.inbox.reply_policy import reply_eligibility

        replies: list[InboxReplyResponse] = []
        if include_replies:
            if "replies" in getattr(message, "_prefetched_objects_cache", {}):
                rows = message.replies.all()
            else:
                rows = message.replies.select_related("author")
            replies = [InboxReplyResponse.from_reply(r) for r in rows]
        return cls(
            id=message.id,
            workspace_id=message.workspace_id,
            social_account_id=message.social_account_id,
            platform=message.social_account.display_platform,
            transport=message.social_account.transport,
            message_type=message.message_type,
            status=message.status,
            sentiment=message.sentiment,
            sender_name=message.sender_name,
            sender_handle=message.sender_handle or "",
            body=message.body or "",
            related_post_id=message.related_post_id,
            received_at=message.received_at,
            created_at=message.created_at,
            replies=replies,
            attachments=message.attachments,
            reply_eligibility=reply_eligibility(message),
        )


class InboxMessagesListResponse(Schema):
    messages: list[InboxMessageResponse]
    limit: int
    next_cursor: str | None = None


class CreateReplyRequest(Schema):
    body: str = Field(..., min_length=1, max_length=10_000, description="The reply text.")
    idempotency_key: str | None = Field(
        None,
        max_length=128,
        description=(
            "Optional client-chosen retry key. When both this field and the "
            "`Idempotency-Key` header are sent, this field wins. Same key + same "
            "body → the first response is replayed verbatim; same key + different "
            "body → 422; a concurrent request holding the same key → 409. "
            "Keys live 24h and are scoped to this API key."
        ),
    )
    send: bool = Field(
        False,
        description=(
            "When true, immediately deliver the reply to the platform instead of "
            "leaving it as a draft. Requires the ``reply_from_inbox`` permission."
        ),
    )


class UpdateReplyRequest(Schema):
    body: str = Field(..., min_length=1, max_length=10_000, description="Replacement reply text.")


# ---------------------------------------------------------------------------
# Error envelope (used by the exception handler in api.py)
# ---------------------------------------------------------------------------


class ErrorResponse(Schema):
    error: str
    detail: str | None = None
    tier: str | None = None
    limit: int | None = None
    remaining: int | None = None
    retry_after: int | None = None
    reset_at: dt.datetime | None = None


# ---------------------------------------------------------------------------
# /live — YouTube Live broadcasts
# ---------------------------------------------------------------------------

LivePrivacyStatus = Literal["public", "unlisted", "private"]
LiveResolution = Literal["2160p", "1080p", "720p", "480p", "360p", "240p", "144p"]
LiveFrameRate = Literal["30fps", "60fps"]
LiveTransitionAction = Literal["test", "go_live", "stop"]


class LiveBroadcastCreateRequest(Schema):
    social_account_id: uuid.UUID = Field(
        ...,
        description="A connected YouTube account in this key's allowlist (see GET /accounts/).",
    )
    title: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Broadcast title. Shown to viewers; also used as the ingest stream title.",
    )
    description: str = Field("", max_length=5000, description="Optional broadcast description.")
    privacy_status: LivePrivacyStatus = Field(
        "private",
        description="Default private: nothing goes live until you transition it, and the VOD stays private.",
    )
    resolution: LiveResolution = Field("1080p", description="Ingest resolution.")
    frame_rate: LiveFrameRate = Field("30fps", description="Ingest frame rate.")
    scheduled_start: dt.datetime | None = Field(
        None,
        description="Optional scheduled start (ISO 8601). A tz-less value is read as UTC.",
    )
    idempotency_key: str | None = Field(
        None,
        max_length=128,
        description=(
            "Optional client-chosen retry key. When both this field and the "
            "`Idempotency-Key` header are sent, this field wins. Same key + same "
            "body → the first response is replayed verbatim (including the same "
            "stream key — the retry is the same logical create); same key + "
            "different body → 422; a concurrent request holding the same key → "
            "409. Keys live 24h and are scoped to this API key."
        ),
    )


class LiveTransitionRequest(Schema):
    action: LiveTransitionAction = Field(
        ...,
        description="'test' → testing (preview), 'go_live' → live, 'stop' → complete.",
    )
    confirm: bool = Field(
        False,
        description=(
            "Required when going live on a PUBLIC broadcast: pass true to confirm viewers "
            "will be able to watch immediately. Private/unlisted broadcasts go live without it."
        ),
    )


class LiveBroadcastResponse(Schema):
    id: uuid.UUID
    broadcast_id: str
    stream_id: str
    social_account_id: uuid.UUID
    social_account_name: str
    title: str
    description: str
    privacy_status: str
    status: str
    scheduled_start: dt.datetime | None
    actual_start: dt.datetime | None
    actual_end: dt.datetime | None
    stream_status: str
    health_status: str
    viewers: int | None
    uptime_seconds: int | None
    watch_url: str
    rtmps_url: str
    masked_stream_key: str
    stream_key: str | None = Field(
        None,
        description=(
            "The full RTMP stream key — present ONLY in the create / recreate-stream response, "
            "exactly once. Never returned by list/detail. Copy it into OBS immediately."
        ),
    )
    key_shown: bool
    last_error: str
    last_checked_at: dt.datetime | None
    created_at: dt.datetime
    updated_at: dt.datetime

    @field_serializer(
        "scheduled_start", "actual_start", "actual_end", "last_checked_at", "created_at", "updated_at"
    )
    def _serialize_dt(self, value: dt.datetime | None) -> str | None:
        return _serialize_utc_z(value)

    @classmethod
    def from_broadcast(cls, broadcast, *, stream_key: str | None = None) -> LiveBroadcastResponse:
        """Single source of truth for LiveBroadcast → API response shape.

        Used by both the REST router and MCP handlers so the two surfaces
        cannot drift. ``stream_key`` is the full key, passed only by the
        create / recreate responses that disclose it once.
        """
        return cls(
            id=broadcast.id,
            broadcast_id=broadcast.broadcast_id,
            stream_id=broadcast.stream_id or "",
            social_account_id=broadcast.social_account_id,
            social_account_name=getattr(broadcast.social_account, "account_name", "") or "",
            title=broadcast.title,
            description=broadcast.description,
            privacy_status=broadcast.privacy_status,
            status=broadcast.status,
            scheduled_start=broadcast.scheduled_start,
            actual_start=broadcast.actual_start,
            actual_end=broadcast.actual_end,
            stream_status=broadcast.last_stream_status,
            health_status=broadcast.last_health_status,
            viewers=broadcast.last_viewers,
            uptime_seconds=broadcast.uptime,
            watch_url=broadcast.watch_url,
            rtmps_url=rtmps_ingestion_url(broadcast.stream_url or ""),
            masked_stream_key=broadcast.masked_stream_key,
            stream_key=stream_key,
            key_shown=broadcast.key_shown,
            last_error=broadcast.last_error,
            last_checked_at=broadcast.last_checked_at,
            created_at=broadcast.created_at,
            updated_at=broadcast.updated_at,
        )


class LiveBroadcastListResponse(Schema):
    broadcasts: list[LiveBroadcastResponse]
    limit: int
    next_cursor: str | None = None


class LiveDeleteResponse(Schema):
    id: uuid.UUID
    broadcast_id: str
    deleted: bool = True
