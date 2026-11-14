"""Composer service helpers.

Where reusable "build me a post" logic lives so it can be called from the
HTMX-driven composer view *and* from the Agent API without duplicating
state-machine semantics.

The Phase 1 helper ``sync_post_scheduled_at`` keeps ``Post.scheduled_at``
in sync with the earliest per-platform ``PlatformPost.scheduled_at``;
the Phase 2 additions wrap ``Post`` + ``PlatformPost`` creation and
status transitions so audit + idempotency live in one place.
"""

from __future__ import annotations

import copy
import datetime as dt
import uuid
from collections.abc import Iterable
from typing import Any


def sync_post_scheduled_at(post):
    """Keep ``Post.scheduled_at`` in sync with the earliest child time.

    Three cases:

    * Some children have a ``scheduled_at`` → set parent to the min.
    * No children have a ``scheduled_at`` AND parent has one cached →
      clear the parent so listings, calendars, and Coalesce fallbacks
      don't keep showing the post as scheduled. This branch covers the
      "agent cancels the last scheduled child" flow — without it,
      ``Post.scheduled_at`` lingered at the old value and the post
      kept appearing as scheduled in the UI.
    * No children, parent already null → no-op.

    A post that gains a committed schedule also drops any draft-stage
    ``proposed_publish_at``: this is the chokepoint the scheduling paths flow
    through (``create_post``, ``transition_platform_post`` for the REST
    ``/schedule`` route and the MCP ``schedule_draft`` tool, and the PATCH
    re-time which calls this after saving), so the suggestion and a real
    schedule never coexist. The ``scheduled_at`` aggregate logic is unchanged —
    the publisher's ``Coalesce(scheduled_at, post__scheduled_at)`` fallback
    depends on it — so only the proposal clear is added here. The one path that
    commits a child WITHOUT a ``scheduled_at`` (the composer's per-account chip
    endpoint) clears the proposal itself rather than routing through here, to
    avoid disturbing that aggregate.
    """
    times = list(post.platform_posts.exclude(scheduled_at__isnull=True).values_list("scheduled_at", flat=True))
    if not times:
        if post.scheduled_at is not None:
            post.scheduled_at = None
            post.save(update_fields=["scheduled_at", "updated_at"])
        return
    earliest = min(times)
    update_fields = []
    if post.scheduled_at != earliest:
        post.scheduled_at = earliest
        update_fields.append("scheduled_at")
    if post.proposed_publish_at is not None:
        post.proposed_publish_at = None
        update_fields.append("proposed_publish_at")
    if update_fields:
        post.save(update_fields=[*update_fields, "updated_at"])


# ---------------------------------------------------------------------------
# Phase 2 — Agent-API-facing post creation
# ---------------------------------------------------------------------------


_INITIAL_STATUSES = {"draft", "scheduled"}


def _zernio_state_extra(*, status: str, scheduled_at: dt.datetime | None) -> dict[str, Any]:
    """The F12 draft/schedule keys a via-Zernio ``PlatformPost`` carries.

    The provider side (``providers/zernio.py build_post_payload``) reads
    exactly these two keys out of ``platform_extra``: ``is_draft`` →
    Zernio ``isDraft``, ``scheduled_for`` → Zernio ``scheduledFor``; with
    neither set the payload says ``publishNow``. A draft row is never
    handed to the engine, so ``is_draft`` is a safety net for any path
    that publishes a row without re-checking its status; a scheduled row
    carries its effective publish time so Zernio holds it until then even
    if our engine fires early. Any other status carries neither key.
    """
    if status == "draft":
        return {"is_draft": True}
    if status == "scheduled" and scheduled_at is not None:
        return {"scheduled_for": scheduled_at.isoformat()}
    return {}


def sync_zernio_draft_schedule_extra(platform_post, *, save: bool = True) -> bool:
    """Rewrite a via-Zernio row's F12 keys to match its current state.

    Only rows whose account publishes through Zernio are touched — a
    direct account's ``platform_extra`` is returned unchanged, so its
    providers never see keys they don't read. A ``scheduled`` row takes
    its own ``scheduled_at``, falling back to the parent ``Post``'s
    aggregate when the row itself carries no time (the composer's
    per-account chip schedules without stamping the row; the publisher
    resolves the same fallback). Any other status drops both keys, so a
    row that leaves draft/scheduled can never publish as a stale draft
    or at a stale time.

    Returns True when ``platform_extra`` changed. With ``save=True``
    (default) the change is persisted immediately; with ``save=False``
    the caller owns the save (e.g. ``transition_platform_post``, which
    folds it into its own ``update_fields`` save).
    """
    account = platform_post.social_account
    via_zernio = account.platform == "zernio" and bool(getattr(account, "is_via_zernio", False))
    if not via_zernio:
        return False
    when = None
    if platform_post.status == "scheduled":
        when = platform_post.scheduled_at or getattr(platform_post.post, "scheduled_at", None)
    wanted = _zernio_state_extra(status=platform_post.status, scheduled_at=when)
    extra = dict(platform_post.platform_extra or {})
    new_extra = {k: v for k, v in extra.items() if k not in ("is_draft", "scheduled_for")}
    new_extra.update(wanted)
    if new_extra == extra:
        return False
    platform_post.platform_extra = new_extra
    if save:
        platform_post.save(update_fields=["platform_extra", "updated_at"])
    return True


def create_post(
    *,
    workspace,
    social_account,
    caption: str,
    media_asset_ids: Iterable[Any] | None = None,
    title: str = "",
    first_comment: str = "",
    internal_notes: str = "",
    scheduled_at: dt.datetime | None = None,
    proposed_publish_at: dt.datetime | None = None,
    author=None,
    status: str = "draft",
    platform_overrides: dict[Any, dict[str, str | None]] | None = None,
    youtube_privacy_status: str | None = None,
    youtube_tags: Iterable[str] | None = None,
    youtube_thumbnail_asset_id: Any | None = None,
    youtube_category_id: str | None = None,
    youtube_made_for_kids: bool | None = None,
):
    """Create a ``Post`` + one ``PlatformPost`` for ``social_account``.

    The canonical service-layer entry point for the Agent API. The
    composer's HTMX-driven ``save_post`` view will be refactored to call
    this in a follow-up; for now both code paths coexist and only the
    API uses it.

    Contract:

    * ``status`` must be one of ``draft`` or ``scheduled``. Other
      states (``approved``, ``publishing``, …) are produced only via
      explicit transitions through ``transition_platform_post``.
    * If ``status='scheduled'``, ``scheduled_at`` is required and must
      be in the future. The publisher polls every ~15 s; a past
      ``scheduled_at`` is accepted and will fire on the very next tick,
      but the caller almost certainly meant "now-ish" so we leave the
      decision to them.
    * ``proposed_publish_at`` is an optional draft-stage suggestion. It is
      stored as-is regardless of ``status`` and carries no future-time
      requirement — it never reaches the publisher.
    * ``social_account.workspace_id`` must equal ``workspace.id``. The
      caller (auth class / view) already enforced scope, but we
      double-check here so anyone reaching the service directly can't
      bypass it.
    * Media assets are attached in order by their position in
      ``media_asset_ids``. They must already live in the workspace's
      media library.
    * YouTube-only options (``youtube_*``) are stored on the
      ``PlatformPost.platform_extra`` using exactly the keys the
      publisher engine / YouTube provider read — ``privacy_status``,
      ``tags``, ``category_id``, ``thumbnail_asset_id`` and
      ``self_declared_made_for_kids`` (note: the provider reads
      ``self_declared_made_for_kids``, never ``made_for_kids``) — and
      only when the destination platform is YouTube, only when the
      option was actually supplied. Via Zernio only ``privacy_status``
      and ``tags`` are kept (Zernio has no thumbnail / made-for-kids /
      category field — mirrors the composer view and
      ``SocialAccount.ZERNIO_ADVANCED_FIELDS``). A
      ``youtube_thumbnail_asset_id`` must reference a ``MediaAsset``
      in the same workspace or ``ValueError`` is raised (→ 422 on the
      REST route). Omitting every ``youtube_*`` option leaves
      ``platform_extra`` empty and publish-time defaults apply
      (public, no tags, no thumbnail, category ``"22"``, not made
      for kids).
    * F12 (write side): when the destination account publishes
      through Zernio — any destination platform, including YouTube
      via Zernio — ``platform_extra`` also records the draft/schedule
      state the Zernio provider reads: ``is_draft: True`` for a
      ``draft``, ``scheduled_for`` (ISO 8601 of ``scheduled_at``) for
      a ``scheduled`` post. Direct destinations never get these keys.
      ``transition_platform_post`` (and the composer's own transitions)
      re-derive them via ``sync_zernio_draft_schedule_extra`` so a
      draft scheduled later doesn't keep a stale ``is_draft``.

    Returns the persisted ``Post`` with one ``PlatformPost`` child.
    """
    from django.db import transaction

    from apps.composer.models import PlatformPost, Post, PostMedia
    from apps.media_library.models import MediaAsset

    if status not in _INITIAL_STATUSES:
        raise ValueError(
            f"create_post can only initialise status in {sorted(_INITIAL_STATUSES)}; "
            f"got {status!r}. Use transition_platform_post for other states."
        )
    if status == "scheduled" and scheduled_at is None:
        raise ValueError("status='scheduled' requires scheduled_at.")
    if social_account.workspace_id != workspace.id:
        raise ValueError(f"SocialAccount {social_account.id} is not in workspace {workspace.id}.")

    # Refuse to queue work against a connection the platform itself has
    # rejected. ``failed`` rows still count against the platform quota
    # (see ``QUOTA_CONSUMING_STATUSES`` in apps/api/limits.py), so each
    # silently-scheduled post against a disconnected/errored account
    # would burn one slot without ever publishing. Forcing an explicit
    # error here makes the agent reconnect first.
    if getattr(social_account, "needs_reconnect", False):
        raise ValueError(
            f"SocialAccount {social_account.id} is in connection_status "
            f"{social_account.connection_status!r}; reconnect it before scheduling."
        )

    if status == "scheduled":
        _require_approval_gate_passes(workspace)

    media_ids = list(media_asset_ids or [])
    # Pull and validate all referenced assets in a single query — fail
    # closed if any ID is missing or belongs to a different workspace.
    #
    # Normalize every id to a ``UUID`` for keying/lookup so all caller shapes
    # match identically: UUID objects (REST, coerced by Pydantic) and UUID
    # *strings* in any form (MCP, off the JSON-RPC wire — including uppercase or
    # braced). A raw string never compares equal to a UUID key, so without this
    # MCP-supplied assets are wrongly rejected as "not found in workspace".
    asset_map: dict[uuid.UUID, MediaAsset] = {}
    resolved: list[tuple[Any, uuid.UUID | None]] = []
    if media_ids:

        def _to_uuid(value):
            try:
                return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
            except (ValueError, TypeError, AttributeError):
                return None

        resolved = [(mid, _to_uuid(mid)) for mid in media_ids]
        found = list(MediaAsset.objects.filter(id__in=[u for _, u in resolved if u is not None], workspace=workspace))
        asset_map = {a.id: a for a in found}
        missing = [mid for mid, u in resolved if u is None or u not in asset_map]
        if missing:
            raise ValueError(f"Media asset(s) not found in workspace {workspace.id}: {missing}")

    # YouTube-only options → platform_extra (see contract above).
    # Validated here, before any row is written, so a bad thumbnail id
    # fails closed with no Post / PlatformPost left behind.
    destination = getattr(social_account, "display_platform", None) or social_account.platform
    via_zernio = social_account.platform == "zernio" and bool(getattr(social_account, "is_via_zernio", False))
    youtube_extra: dict[str, Any] = {}
    if destination == "youtube":
        if youtube_privacy_status is not None:
            if youtube_privacy_status not in ("public", "unlisted", "private"):
                raise ValueError(
                    "youtube_privacy_status must be one of 'public', 'unlisted', 'private'; "
                    f"got {youtube_privacy_status!r}."
                )
            youtube_extra["privacy_status"] = youtube_privacy_status
        if youtube_tags:
            cleaned_tags: list[str] = []
            seen_tags: set[str] = set()
            for raw_tag in youtube_tags:
                tag = str(raw_tag).strip()
                if tag and tag not in seen_tags:
                    seen_tags.add(tag)
                    cleaned_tags.append(tag)
            if cleaned_tags:
                youtube_extra["tags"] = cleaned_tags
        if not via_zernio:
            if youtube_category_id is not None:
                youtube_extra["category_id"] = youtube_category_id
            if youtube_made_for_kids is not None:
                youtube_extra["self_declared_made_for_kids"] = bool(youtube_made_for_kids)
            if youtube_thumbnail_asset_id is not None:
                try:
                    thumb_uuid = (
                        youtube_thumbnail_asset_id
                        if isinstance(youtube_thumbnail_asset_id, uuid.UUID)
                        else uuid.UUID(str(youtube_thumbnail_asset_id))
                    )
                except (ValueError, TypeError, AttributeError):
                    thumb_uuid = None
                thumb_asset = (
                    MediaAsset.objects.filter(id=thumb_uuid, workspace=workspace).first()
                    if thumb_uuid is not None
                    else None
                )
                if thumb_asset is None:
                    raise ValueError(
                        f"Thumbnail asset {youtube_thumbnail_asset_id} not found in workspace {workspace.id}."
                    )
                youtube_extra["thumbnail_asset_id"] = str(thumb_asset.id)

    # F12 (write side): a via-Zernio destination — any destination
    # platform — also records the draft/schedule state the Zernio
    # provider reads out of platform_extra. Direct destinations never
    # get these keys; their providers don't read them.
    if via_zernio:
        youtube_extra.update(
            _zernio_state_extra(
                status=status,
                scheduled_at=scheduled_at if status == "scheduled" else None,
            )
        )

    if status == "scheduled":
        # A new post has no platform_extra, so a Pinterest pin has no board
        # yet and is refused here: it can be created as a draft, given a
        # board in the composer, and scheduled from there.
        reason = publish_blocker(
            social_account,
            media_types=usable_media_types(asset_map.values()),
            processing_statuses=[a.processing_status for a in asset_map.values() if a.file],
        )
        if reason:
            raise ValueError(reason)

    override = (platform_overrides or {}).get(social_account.id) or {}

    with transaction.atomic():
        post = Post.objects.create(
            workspace=workspace,
            author=author,
            title=title,
            caption=caption,
            first_comment=first_comment,
            internal_notes=internal_notes,
            scheduled_at=scheduled_at if status == "scheduled" else None,
            # A proposal is plain draft metadata — keep it independent of
            # ``status`` (unlike ``scheduled_at``, which only exists once the
            # post is committed to the publisher).
            proposed_publish_at=proposed_publish_at,
        )
        # Each override field is independent: ``None`` (or omitted)
        # means "no platform-specific override; fall back to the post
        # value via ``PlatformPost.effective_*``". Any string —
        # including ``""`` — is treated as an explicit override.
        PlatformPost.objects.create(
            post=post,
            social_account=social_account,
            status=status,
            scheduled_at=scheduled_at if status == "scheduled" else None,
            platform_specific_title=override.get("title"),
            platform_specific_caption=override.get("caption"),
            platform_specific_first_comment=override.get("first_comment"),
            platform_extra=youtube_extra,
        )
        for position, (_mid, u) in enumerate(resolved):
            # ``u`` is validated non-None and present in ``asset_map`` above
            # (the ``missing`` check raises otherwise); narrow for mypy.
            assert u is not None
            PostMedia.objects.create(
                post=post,
                media_asset=asset_map[u],
                position=position,
            )

    # Keep the aggregate column consistent — the publisher polls
    # PlatformPost directly so this is just for listings/UI, but the
    # composer view code relies on it.
    sync_post_scheduled_at(post)
    return post


def transition_platform_post(
    platform_post,
    target_status: str,
    *,
    scheduled_at: dt.datetime | None = None,
):
    """Apply a state-machine transition and persist.

    Wraps ``PlatformPost.transition_to`` (which only mutates the model
    in memory) with the persistence + side-effects the API needs:

    * Save the new status.
    * On ``scheduled``, set ``scheduled_at`` if provided, and sync the
      aggregate ``Post.scheduled_at`` column.
    * On ``draft``, clear ``scheduled_at`` so the row stops being a
      target for the publisher's polling query.

    Raises ``ValueError`` (from ``transition_to``) if the transition
    isn't in ``VALID_TRANSITIONS``.
    """
    from django.db import transaction

    from apps.composer.models import PlatformPost

    # Approval workflow gate — Codex review (PR #53) flagged that the
    # ``POST /posts/{id}/schedule`` route reached this function with
    # ``target_status="scheduled"`` and bypassed the approval check
    # ``create_post`` enforces. The gate belongs here so EVERY path that
    # promotes a draft to scheduled (REST `/schedule` route, any future
    # MCP transition tool, the composer's HTMX `transition_platform_post`
    # view) is covered.
    if target_status == "scheduled":
        _require_approval_gate_passes(platform_post.post.workspace)
        require_publishable(platform_post)

    with transaction.atomic():
        platform_post.transition_to(target_status)
        # Always include ``updated_at`` — Django docs: when ``update_fields``
        # is explicit, ``auto_now`` fields are NOT touched unless listed.
        # Without this every API/MCP-driven transition would leave
        # ``PlatformPost.updated_at`` frozen at creation time, breaking
        # any "modified since X" sync and ORDER BY updated_at view.
        # TRANSITION_FIELDS covers everything transition_to writes (the retry
        # budget and the publish handle, not just status), so the REST/MCP
        # scheduling path persists the same reset the composer does.
        update_fields = {*PlatformPost.TRANSITION_FIELDS, "updated_at"}
        if target_status == "scheduled":
            if scheduled_at is not None:
                platform_post.scheduled_at = scheduled_at
                update_fields.add("scheduled_at")
        elif target_status == "draft":
            if platform_post.scheduled_at is not None:
                platform_post.scheduled_at = None
                update_fields.add("scheduled_at")
            # A drafted child is no longer queued — drop its QueueEntry so the
            # slot frees as a gap. Covers cancel / unschedule paths that would
            # otherwise orphan the row with a stale assigned_slot_datetime.
            from apps.calendar.models import QueueEntry

            QueueEntry.objects.filter(
                post=platform_post.post,
                queue__social_account=platform_post.social_account,
            ).delete()
        elif target_status == "published":
            update_fields.add("published_at")  # set by transition_to
        # F12: a via-Zernio row's draft/schedule keys follow the status
        # this transition just set — a draft scheduled here must stop
        # saying is_draft, and a scheduled row sent back to draft must
        # stop advertising its old scheduled_for. Folded into this
        # save (no extra write when nothing changed).
        if sync_zernio_draft_schedule_extra(platform_post, save=False):
            update_fields.add("platform_extra")
        platform_post.save(update_fields=list(update_fields))

    sync_post_scheduled_at(platform_post.post)
    return platform_post


def clone_post(post, *, author=None):
    """Create a fresh DRAFT duplicate of *post* (Clone / Repost).

    Copies caption, title, first comment, internal notes, tags, category, every
    per-platform override (``platform_specific_*`` + ``platform_extra``) and all
    media attachments. The copy starts as a ``draft`` with no schedule, so a
    published (or any) post can be re-edited and re-queued without touching the
    original. ``author`` defaults to the source author.
    """
    from django.db import transaction

    from apps.composer.models import PlatformPost, Post, PostMedia
    from apps.social_accounts.youtube_compliance import authored_youtube_settings

    with transaction.atomic():
        new_post = Post.objects.create(
            workspace=post.workspace,
            author=author or post.author,
            title=(f"Copy of {post.title}" if post.title else ""),
            caption=post.caption,
            first_comment=post.first_comment,
            internal_notes=post.internal_notes,
            tags=post.tags,
            category=post.category,
        )
        children = list(post.platform_posts.all())
        if children:
            created_children = PlatformPost.objects.bulk_create(
                [
                    PlatformPost(
                        post=new_post,
                        social_account=pp.social_account,
                        status="draft",
                        platform_specific_title=pp.platform_specific_title,
                        platform_specific_caption=pp.platform_specific_caption,
                        platform_specific_first_comment=pp.platform_specific_first_comment,
                        platform_specific_media=copy.deepcopy(pp.platform_specific_media),
                        platform_extra=copy.deepcopy(
                            authored_youtube_settings(pp.platform_extra)
                            if pp.social_account.display_platform == "youtube"
                            else pp.platform_extra or {}
                        ),
                    )
                    for pp in children
                ]
            )
            # A clone always starts as a draft: a source row's
            # scheduled_for must not survive onto the copy, and a
            # via-Zernio copy records is_draft like any other draft.
            for child in created_children:
                sync_zernio_draft_schedule_extra(child)
        media = list(post.media_attachments.all())
        if media:
            PostMedia.objects.bulk_create(
                [
                    PostMedia(
                        post=new_post,
                        media_asset=pm.media_asset,
                        position=pm.position,
                        alt_text=pm.alt_text,
                        platform_overrides=copy.deepcopy(pm.platform_overrides),
                    )
                    for pm in media
                ]
            )
    return new_post


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


#: Workspace approval modes that BLOCK a direct draft → scheduled
#: transition. The composer's HTMX path routes around this via a
#: separate ``submit_for_approval`` action; the Agent API surface uses
#: the same service layer so the constraint lives here once.
_APPROVAL_MODES_BLOCKING_DIRECT_SCHEDULE = frozenset({"required_internal", "required_internal_and_client"})


def _require_approval_gate_passes(workspace) -> None:
    """Raise ``ValueError`` if the workspace forbids direct scheduling.

    Drafts are always allowed (they're explicit save-for-later);
    scheduling implies "ready to publish," which is exactly what the
    approval workflow gates.
    """
    if getattr(workspace, "approval_workflow_mode", "none") in _APPROVAL_MODES_BLOCKING_DIRECT_SCHEDULE:
        raise ValueError(
            "Workspace requires approval before scheduling; create the post as a "
            "draft and route it through the approval workflow."
        )


def usable_media_types(assets) -> set[str]:
    """The media types among ``assets`` the publisher will actually upload.

    It skips an attachment whose asset has no stored file, so one of those
    can't satisfy a platform that needs media — or a video.
    """
    return {asset.media_type for asset in assets if asset.file}


def _usable_attachments():
    """``PostMedia`` rows ``usable_media_types`` would count, as a queryset."""
    from apps.composer.models import PostMedia

    return PostMedia.objects.exclude(media_asset__file="")


def post_media_types(post, *, excluding=None) -> set[str]:
    """``usable_media_types`` for ``post``'s attachments, in one query.

    ``excluding`` (a ``PostMedia``) answers for the post as it would be once
    that attachment is removed.
    """
    attachments = _usable_attachments().filter(post_id=post.pk)
    if excluding is not None:
        attachments = attachments.exclude(pk=excluding.pk)
    return set(attachments.values_list("media_asset__media_type", flat=True).distinct())


def media_types_by_post(post_ids) -> dict:
    """``post_media_types`` for many posts in one query; absent ids have none."""
    by_post: dict = {}
    rows = _usable_attachments().filter(post_id__in=post_ids).values_list("post_id", "media_asset__media_type")
    for post_id, media_type in rows.distinct():
        by_post.setdefault(post_id, set()).add(media_type)
    return by_post


def post_media_processing_statuses(post, *, excluding=None) -> set[str]:
    """``processing_status`` values of ``post``'s usable attachments.

    Mirrors ``post_media_types``: only attachments with a stored file
    count — an asset without a file is already excluded from the media
    the publisher can use, so its processing state is irrelevant.
    """
    attachments = _usable_attachments().filter(post_id=post.pk)
    if excluding is not None:
        attachments = attachments.exclude(pk=excluding.pk)
    return set(attachments.values_list("media_asset__processing_status", flat=True).distinct())


def media_blocker(social_account, *, media_types) -> str:
    """Why ``social_account`` can't publish a post with these media, or "".

    TikTok and YouTube publish only video; Instagram and Pinterest need an
    image or a video. Their providers refuse anything else before calling out.
    """
    from apps.media_library.models import MediaAsset
    from providers import is_video_only, requires_media

    platform = social_account.display_platform_label
    if is_video_only(social_account.display_platform) and MediaAsset.MediaType.VIDEO not in media_types:
        return f"{platform} can only publish videos. Add a video to this post before scheduling it."
    if requires_media(social_account.display_platform) and not media_types:
        return f"{platform} needs an image or a video. Add one to this post before scheduling it."
    return ""


def publish_blocker(
    social_account,
    *,
    media_types,
    platform_extra: dict | None = None,
    processing_statuses: Iterable[str] | None = None,
) -> str:
    """Why a post can't publish to ``social_account`` as it stands, or "".

    The one statement of this rule. The publisher checks it before calling a
    platform, and every path that schedules a post checks it first so the user
    hears about it then rather than after the post's other platforms have gone
    out. Only failures that are certain before the platform is ever called: the
    media a platform can't do without (``media_blocker``), a media asset that
    is still being processed (its duration/thumbnail aren't final yet, so the
    platform would fetch an unfinished file), and a pin with no board, which
    our own Pinterest provider refuses.

    ``processing_statuses`` is the ``processing_status`` of each attached
    asset that has a stored file; ``None`` means the caller has no asset
    info (e.g. the CSV import's board-only check) and keeps the old
    behaviour — the gate is skipped. Any status other than ``completed``
    blocks.
    """
    from apps.media_library.models import MediaAsset

    reason = media_blocker(social_account, media_types=media_types)
    if reason:
        return reason
    if processing_statuses is not None and any(
        status != MediaAsset.ProcessingStatus.COMPLETED for status in processing_statuses
    ):
        return "Media is still processing; wait until processing completes before scheduling."
    # Raw platform on purpose: Zernio has no boards, so a Zernio-routed
    # Pinterest post must never be blocked for missing one.
    if social_account.platform == "pinterest" and not (platform_extra or {}).get("board_id"):
        return "Pinterest needs a board for every pin. Choose one for this post in the composer before scheduling it."
    return ""


def require_publishable(platform_post, *, media_types=None, processing_statuses=None) -> None:
    """Raise ``ValueError`` if ``platform_post`` is certain to fail at publish time.

    ``media_types`` lets a caller checking many rows answer it for all of them
    in one query (``media_types_by_post``) instead of one per row.
    ``processing_statuses`` is the same idea for the processing gate; when
    omitted it is read off the post's own attachments (one query via
    ``post_media_processing_statuses``), so every existing caller — the REST
    ``/schedule`` route, the MCP ``schedule_draft`` tool, the composer's
    per-account transition endpoint and the calendar drop/promote paths —
    gets the gate without changing its call.
    """
    if media_types is None:
        media_types = post_media_types(platform_post.post)
    if processing_statuses is None:
        processing_statuses = post_media_processing_statuses(platform_post.post)
    reason = publish_blocker(
        platform_post.social_account,
        media_types=media_types,
        platform_extra=platform_post.platform_extra,
        processing_statuses=processing_statuses,
    )
    if reason:
        raise ValueError(reason)


def media_change_blocker(post, *, media_types_after) -> str:
    """Why a media change would strand a scheduled row, or "".

    Scheduling checks the post as it stood then; this stops the media a
    platform needs being taken away afterwards — the video from a TikTok post,
    the last image from a pin — which would leave the row queued to fail. Only
    rows already ``scheduled`` count; anything else is checked again when it is
    scheduled.
    """
    from apps.composer.models import PlatformPost
    from providers import is_video_only

    children = post.platform_posts.filter(status=PlatformPost.Status.SCHEDULED).select_related("social_account")
    for child in children:
        if media_blocker(child.social_account, media_types=media_types_after):
            platform = child.social_account.display_platform_label
            need = "a video" if is_video_only(child.social_account.display_platform) else "an image or a video"
            return (
                f"This post is scheduled to {platform}, which needs {need} on it. "
                f"Keep one on the post, or unschedule it for {platform} first."
            )
    return ""
