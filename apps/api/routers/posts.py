"""``/api/v1/posts/*`` — create, read, update, schedule, cancel.

Every write path:

1. Enforces HTTP-level rate limits (per-key, per-workspace, global).
2. Replays the response if an idempotency key matches an earlier request.
3. Checks workspace permission via the shared ``@require_permission`` helper.
4. Validates the target ``SocialAccount`` is in the key's allowlist.
5. Checks per-platform 24h quota.
6. Calls the composer service layer (single source of truth for state).
7. Writes an audit log row.
8. Persists the response under the idempotency key if one was passed.

This ordering means an expensive operation (the create) can't run until
both the cheap rate-limit check and the issuer-permission check pass —
defence against spending DB cycles for unauthorized callers.
"""

from __future__ import annotations

import uuid

from django.db.models import Count
from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from ninja import Query, Router
from ninja.errors import HttpError

from apps.api.limits import check_platform_quota, enforce_http_rate_limits
from apps.api.middleware import (
    claim_idempotency_slot,
    finalize_idempotent_response,
    fingerprint_request,
    log_audit_entry,
    release_idempotent_claim,
)
from apps.api.pagination import decode_offset_cursor, encode_offset_cursor
from apps.api.schemas import (
    CreatePostRequest,
    PostDeleteResponse,
    PostResponse,
    PostsListResponse,
    ScheduleRequest,
    UpdatePostRequest,
)
from apps.composer.models import PlatformPost, Post
from apps.composer.services import (
    create_post,
    media_change_blocker,
    sync_post_scheduled_at,
    transition_platform_post,
    usable_media_types,
)
from apps.social_accounts.models import SocialAccount

router = Router(tags=["posts"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_perm(request: HttpRequest, key: str) -> None:
    """Re-check a workspace permission inside a Ninja route body.

    We can't decorate Ninja routes with ``@require_permission`` because it
    expects a Django view signature; instead we inline the same check
    against the virtual membership shim.
    """
    membership = getattr(request, "workspace_membership", None)
    if membership is None or not membership.effective_permissions.get(key, False):
        raise HttpError(403, f"Permission denied: {key}")


def _resolve_account(request: HttpRequest, social_account_id: uuid.UUID) -> SocialAccount:
    """Resolve the target account and verify it is in the key's allowlist.

    Defence against confused-deputy attacks: even though the bearer is
    valid, the caller may only act on accounts the issuer explicitly
    listed at issuance time.
    """
    api_key = request.api_key  # type: ignore[attr-defined]  # set by ApiKeyAuth
    allowlist_ids = {sa.id for sa in api_key.social_accounts.all()}
    if social_account_id not in allowlist_ids:
        raise HttpError(403, "SocialAccount is not in this key's allowlist.")
    return SocialAccount.objects.get(id=social_account_id)


def _can_view_internal_notes(request: HttpRequest) -> bool:
    """Whether this caller may see a post's team-only ``internal_notes``.

    The composer hides ``internal_notes`` from the ``client`` / ``viewer``
    workspace roles ([views.py](apps/composer/views.py): ``can_view_internal_notes``).
    Those are exactly the builtin roles without ``create_posts`` (see
    ``BUILTIN_ROLE_PERMISSIONS``), so gating visibility on ``create_posts``
    reproduces that rule uniformly for both API keys and OAuth members. Reads
    (`GET /posts/{id}`) aren't otherwise permission-gated, so without this the
    shared serializer would leak notes to any read-capable client/viewer.
    """
    membership = getattr(request, "workspace_membership", None)
    return bool(membership and membership.effective_permissions.get("create_posts", False))


def _post_to_response(request: HttpRequest, post: Post) -> PostResponse:
    return PostResponse.from_post(post, include_internal_notes=_can_view_internal_notes(request))


def _get_workspace_post(request: HttpRequest, post_id: uuid.UUID) -> Post:
    """Fetch a Post that belongs to the key's workspace **and** whose every
    ``PlatformPost`` child targets a ``SocialAccount`` in the key's
    allowlist.

    The workspace filter alone is not enough — a key scoped to LinkedIn-A
    could otherwise read or mutate a Post whose only child is for
    Twitter-B in the same workspace if it happened to know the Post UUID.
    The "all children in allowlist" rule (rather than "any child") means
    schedule/cancel/update can freely iterate ``post.platform_posts``
    without us having to scope each operation sub-Post — there is no
    foreign child for them to touch.

    We intentionally don't distinguish "doesn't exist" from "exists in
    another workspace" from "exists but partially out of scope" — all
    three return 404 so the API doesn't leak the existence of foreign
    IDs to a partial-scope bearer.
    """
    from django.http import Http404

    post = get_object_or_404(
        Post.objects.prefetch_related("platform_posts__social_account__zernio_binding"),
        id=post_id,
        workspace_id=request.api_key.workspace_id,  # type: ignore[attr-defined]
    )
    allowed_ids = {sa.id for sa in request.api_key.social_accounts.all()}  # type: ignore[attr-defined]
    pp_account_ids = {pp.social_account_id for pp in post.platform_posts.all()}
    # No platform_posts → nothing this key could legitimately act on.
    # Foreign child → leaking even via a read would be a confused-deputy.
    if not pp_account_ids or not pp_account_ids.issubset(allowed_ids):
        raise Http404()
    return post


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post(
    "/",
    response={201: PostResponse, 200: PostResponse},
    summary="Create a draft or scheduled post",
    description=(
        "Idempotency: `idempotency_key` in the body wins over the "
        "`Idempotency-Key` header when both are sent. Same key + same body → "
        "the first response is replayed verbatim; same key + different body → "
        "422; a concurrent request holding the same key → 409. Keys live 24h "
        "and are scoped to this API key. `action: schedule` requires "
        "`scheduled_at` and the `publish_directly` permission."
    ),
)
def create(request, payload: CreatePostRequest):
    enforce_http_rate_limits(request, is_write=True)
    _require_perm(request, "create_posts")
    # Scheduling implies "ready to publish without further human review",
    # which is exactly what ``publish_directly`` gates in the composer
    # ([views.py:797](apps/composer/views.py)). The REST API must mirror
    # that contract — a key issued with only ``create_posts`` can park
    # drafts but cannot send them into the publisher's poll loop.
    if payload.action == "schedule":
        _require_perm(request, "publish_directly")

    # ---- Cheap validation runs BEFORE we touch the idempotency table.
    # The motivation: an early failure here (403, 422) would otherwise
    # leave a placeholder claim row that release-on-error has to clean
    # up — and forgetting one release path locks the agent out for 24h.
    # Doing all "can this request possibly succeed?" checks pre-claim
    # keeps the claim/release pair tight and confined to the post-claim
    # path (platform quota + create_post).
    social_account = _resolve_account(request, payload.social_account_id)
    if payload.action == "schedule" and payload.scheduled_at is None:
        raise HttpError(422, "scheduled_at is required when action='schedule'.")

    # Build the platform_overrides dict and validate that each override's
    # social_account_id matches one of the post's target accounts. In the
    # current single-account API that's only ``payload.social_account_id``;
    # anything else would silently no-op at publish time, so we reject up
    # front. The plan's [Gap 2] section calls this out explicitly.
    platform_overrides: dict = {}
    for ov in payload.platform_overrides:
        if ov.social_account_id != payload.social_account_id:
            raise HttpError(
                422,
                (
                    f"platform_overrides[*].social_account_id must match the post's "
                    f"social_account_id ({payload.social_account_id}); got {ov.social_account_id}."
                ),
            )
        platform_overrides[ov.social_account_id] = {
            "title": ov.title,
            "caption": ov.caption,
            "first_comment": ov.first_comment,
        }

    # ---- Atomic claim-first idempotency. Three early-out branches
    # before we do *any* mutating work, so concurrent identical retries
    # can never both reach create_post:
    #   replay     — prior request already finished, return its response
    #   in_flight  — a concurrent peer holds the slot, return 409
    #   passthrough/claimed — caller proceeds; only the "claimed" path
    #                         must finalize or release before returning.
    fingerprint = fingerprint_request(request.method or "POST", request.path, payload.dict(by_alias=True))
    # Accept the canonical ``Idempotency-Key`` HTTP header (Stripe-style)
    # in addition to the body field. The header is the industry
    # convention — clients that retry on network timeout typically reuse
    # the header but can't easily re-send the same JSON body — so
    # ignoring it would silently break the "retry-safe" contract Codex
    # review (PR #53) flagged. The body field still wins when both are
    # present, so a deliberate caller can override.
    idempotency_key = payload.idempotency_key or request.headers.get("Idempotency-Key") or None
    try:
        disposition, replay_status, replay_body = claim_idempotency_slot(
            api_key=request.api_key,
            idempotency_key=idempotency_key,
            fingerprint=fingerprint,
        )
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    if disposition == "replay":
        return replay_status, replay_body
    if disposition == "in_flight":
        raise HttpError(
            409,
            "An identical request with this idempotency_key is still in flight; retry shortly.",
        )

    # Platform-quota check has to be inside the claim window because it
    # depends on database state that other concurrent claims could
    # change. Drafts are excluded from the count inside
    # ``check_platform_quota`` so creating drafts cannot exhaust the
    # platform's posting cap.
    if payload.action == "schedule":
        try:
            check_platform_quota(social_account)
        except HttpError:
            release_idempotent_claim(api_key=request.api_key, idempotency_key=idempotency_key)
            raise

    # Single try/except covers every step from create_post through
    # finalize_idempotent_response. Codex review found that earlier
    # code committed the Post + PlatformPost via create_post and then
    # built the response / wrote the audit / called finalize OUTSIDE
    # the release path — so a transient DB error during response build
    # or finalize left the idempotency slot wedged in PENDING forever
    # while the work had already succeeded. Folding everything under
    # one ``try / except / release`` closes that window: any exception
    # after the claim releases the slot, the agent can retry, and the
    # retry will reach create_post fresh (or replay if finalize did
    # commit before the failure).
    try:
        post = create_post(
            workspace=request.api_key.workspace,
            social_account=social_account,
            caption=payload.caption,
            media_asset_ids=payload.media_asset_ids,
            title=payload.title,
            first_comment=payload.first_comment,
            internal_notes=payload.internal_notes,
            scheduled_at=payload.scheduled_at,
            # A scheduled post carries a real time, not a proposal — ignore any
            # proposed_publish_at when scheduling so the two never coexist.
            proposed_publish_at=None if payload.action == "schedule" else payload.proposed_publish_at,
            author=request.user if not request.user.is_anonymous else None,
            status="scheduled" if payload.action == "schedule" else "draft",
            platform_overrides=platform_overrides,
            youtube_privacy_status=payload.privacy_status,
            youtube_tags=payload.tags,
            youtube_thumbnail_asset_id=payload.thumbnail_asset_id,
            youtube_category_id=payload.category_id,
            youtube_made_for_kids=payload.made_for_kids,
        )
        body = _post_to_response(request, post)
        status_code = 201
        log_audit_entry(
            request,
            action=f"post.create.{payload.action}",
            target_id=post.id,
            status_code=status_code,
        )
        # ``model_dump(mode='json')`` yields JSON-safe primitives
        # (UUID→str, datetime→ISO-8601 str) so the response_body
        # JSONField round-trips cleanly through psycopg's jsonb adapter.
        finalize_idempotent_response(
            api_key=request.api_key,
            idempotency_key=idempotency_key,
            status_code=status_code,
            body=body.model_dump(mode="json"),
        )
    except ValueError as exc:
        # Use the *effective* idempotency key (header fallback applied)
        # so a header-only client's claim is released too — Codex PR #53
        # round-3 flagged that the previous ``payload.idempotency_key``
        # was None on header-only requests and left the claim wedged in
        # PENDING until the 24h sweep.
        release_idempotent_claim(api_key=request.api_key, idempotency_key=idempotency_key)
        raise HttpError(422, str(exc)) from exc
    except Exception:
        release_idempotent_claim(api_key=request.api_key, idempotency_key=idempotency_key)
        raise
    return status_code, body


@router.get(
    "/",
    response=PostsListResponse,
    summary="List posts visible to this key",
    description=(
        "Newest first. Cursor pagination: pass `limit` (1-100, default 50) and the "
        "`next_cursor` from the previous page. `status` matches the per-platform "
        "child status (identical to the aggregate status for the single-child "
        "posts the API creates). `social_account_id` must be in this key's "
        "allowlist (403 otherwise). A post is listed only when every one of its "
        "platform children is allowlisted — the same visibility rule as "
        "`GET /posts/{id}`."
    ),
)
def list_posts(
    request,
    status: str | None = Query(None, description="Filter by platform-post status, e.g. draft, scheduled, published."),
    social_account_id: uuid.UUID | None = Query(None),
    limit: int = Query(50, ge=1, le=100),
    cursor: str | None = Query(None),
):
    enforce_http_rate_limits(request, is_write=False)
    if status is not None and status not in PlatformPost.Status.values:
        raise HttpError(422, f"status must be one of {', '.join(PlatformPost.Status.values)}")
    try:
        offset = decode_offset_cursor(cursor)
    except ValueError as exc:
        raise HttpError(422, "cursor is not a valid pagination cursor") from exc
    allowed_ids = {sa.id for sa in request.api_key.social_accounts.all()}  # type: ignore[attr-defined]
    if social_account_id is not None and social_account_id not in allowed_ids:
        raise HttpError(403, "social_account_id is not in this key's allowlist.")
    if not allowed_ids:
        return PostsListResponse(posts=[], limit=limit, next_cursor=None)

    # Visibility: exclude any post with a child outside the allowlist, and
    # posts with no children at all (same rule as ``_get_workspace_post``).
    foreign_post_ids = (
        PlatformPost.objects.filter(post__workspace_id=request.api_key.workspace_id)  # type: ignore[attr-defined]
        .exclude(social_account_id__in=allowed_ids)
        .values_list("post_id", flat=True)
    )
    qs = (
        Post.objects.filter(workspace_id=request.api_key.workspace_id)  # type: ignore[attr-defined]
        .exclude(id__in=foreign_post_ids)
        .annotate(_n_children=Count("platform_posts"))
        .filter(_n_children__gt=0)
        .prefetch_related("platform_posts__social_account__zernio_binding")
    )
    if status:
        qs = qs.filter(platform_posts__status=status).distinct()
    if social_account_id is not None:
        qs = qs.filter(platform_posts__social_account_id=social_account_id).distinct()
    qs = qs.order_by("-created_at", "id")

    rows = list(qs[offset : offset + limit + 1])
    has_more = len(rows) > limit
    rows = rows[:limit]
    log_audit_entry(request, action="post.list", target_id=None, status_code=200)
    return PostsListResponse(
        posts=[_post_to_response(request, p) for p in rows],
        limit=limit,
        next_cursor=encode_offset_cursor(offset + limit) if has_more else None,
    )


@router.get("/{post_id}", response=PostResponse, summary="Read a single post")
def retrieve(request, post_id: uuid.UUID):
    enforce_http_rate_limits(request, is_write=False)
    post = _get_workspace_post(request, post_id)
    log_audit_entry(request, action="post.read", target_id=post.id, status_code=200)
    return _post_to_response(request, post)


@router.patch(
    "/{post_id}",
    response=PostResponse,
    summary="Update draft fields",
    description=(
        "Partial update of an editable post (draft / scheduled / review states; "
        "published answers 409). All fields are optional; `null` leaves a field "
        "unchanged, except `tags: []` which clears the stored YouTube tags. "
        "YouTube-only fields (`privacy_status`, `tags`, `thumbnail_asset_id`, "
        "`category_id`, `made_for_kids`) apply to the post's YouTube-destination "
        "children while they are still draft / scheduled — the publisher reads "
        "them at publish time. Sending one with no YouTube child answers 422; "
        "`category_id` / `made_for_kids` / `thumbnail_asset_id` on a Zernio-routed "
        "YouTube child also answer 422."
    ),
)
def update(request, post_id: uuid.UUID, payload: UpdatePostRequest):
    enforce_http_rate_limits(request, is_write=True)
    _require_perm(request, "create_posts")  # mirrors composer's create-or-edit perm
    post = _get_workspace_post(request, post_id)

    # Only allow updates while editable.
    if not post.is_editable:
        raise HttpError(409, f"Post is not editable in status {post.status}.")

    # Re-timing a scheduled post is publish-budget behaviour: pushing
    # ``scheduled_at`` into the past makes the publisher fire the post on
    # its very next poll (~15 s), and pushing it far into the future buries
    # admin-scheduled content. Either is a privilege escalation for a key
    # that doesn't hold ``publish_directly`` — the create / schedule routes
    # and every MCP transition tool gate this exact mutation on that
    # permission, and the PATCH route must do the same to stay consistent.
    # Codex PR #53 security review (round 4) caught this gap.
    if payload.scheduled_at is not None and post.platform_posts.filter(status="scheduled").exists():
        _require_perm(request, "publish_directly")

    # ---- Validate-everything-first.
    #
    # Codex review found two bugs in the previous implementation:
    #   (a) ``scheduled_children.update(scheduled_at=...)`` ran BEFORE
    #       media validation, so a 422 from a foreign media asset still
    #       committed the new schedule timestamp to the DB.
    #   (b) ``post.media_attachments.all().delete()`` + the create loop
    #       were not wrapped in a transaction, so a mid-loop failure
    #       deleted the originals and persisted only a partial new set.
    # Fix: resolve every required reference and reject every invalid
    # input before any database mutation, then perform all mutations
    # inside a single ``transaction.atomic()`` block. Either everything
    # commits or nothing does.
    from django.db import transaction

    from apps.composer.models import PostMedia
    from apps.media_library.models import MediaAsset

    wanted_media: list = []
    resolved_assets: dict = {}
    if payload.media_asset_ids is not None:
        wanted_media = list(payload.media_asset_ids)
        resolved_assets = {a.id: a for a in MediaAsset.objects.filter(id__in=wanted_media, workspace=post.workspace)}
        missing = [i for i in wanted_media if i not in resolved_assets]
        if missing:
            raise HttpError(422, f"Media asset(s) not in workspace: {missing}")
        # A scheduled post may be edited, but not into one it can't publish:
        # swapping a TikTok post's video for an image, or emptying a pin's
        # media, would leave it queued to fail after the schedule route had
        # checked it.
        reason = media_change_blocker(
            post, media_types_after=usable_media_types(resolved_assets[mid] for mid in wanted_media)
        )
        if reason:
            raise HttpError(422, reason)

    # ---- YouTube options (validate-everything-first, same as media).
    # The publisher reads ``PlatformPost.platform_extra`` at publish time
    # (see ``apps/publisher/engine.py``), so updating the extra of a
    # draft/scheduled YouTube child here takes effect without touching
    # the state machine — published/publishing children are history and
    # are never rewritten.
    yt_children: list = []
    yt_requested = any(
        value is not None
        for value in (
            payload.privacy_status,
            payload.tags,
            payload.thumbnail_asset_id,
            payload.category_id,
            payload.made_for_kids,
        )
    )
    if yt_requested:
        yt_children = [
            pp for pp in post.platform_posts.all() if pp.social_account.display_platform == "youtube"
        ]
        if not yt_children:
            raise HttpError(422, "This post has no YouTube destination; YouTube options cannot be patched.")
        yt_children = [pp for pp in yt_children if pp.status not in ("published", "publishing")]
        if not yt_children:
            raise HttpError(422, "No editable YouTube platform post on this post.")
        # Zernio carries no thumbnail / category / made-for-kids field:
        # an explicit PATCH of one must fail loudly instead of silently
        # no-op-ing. (Create drops them; a PATCH exists only to change
        # the field it names, so silent-drop would be a lie.)
        zernio_blocked = [
            name
            for name, value in (
                ("thumbnail_asset_id", payload.thumbnail_asset_id),
                ("category_id", payload.category_id),
                ("made_for_kids", payload.made_for_kids),
            )
            if value is not None
        ]
        if zernio_blocked and any(pp.social_account.is_via_zernio for pp in yt_children):
            raise HttpError(
                422,
                f"{', '.join(zernio_blocked)} is not available for YouTube accounts routed via Zernio.",
            )
        if payload.thumbnail_asset_id is not None:
            thumb = MediaAsset.objects.filter(id=payload.thumbnail_asset_id, workspace=post.workspace).first()
            if thumb is None:
                raise HttpError(
                    422,
                    f"Thumbnail asset {payload.thumbnail_asset_id} not found in workspace {post.workspace_id}.",
                )

    with transaction.atomic():
        update_fields: list[str] = []
        if payload.caption is not None:
            post.caption = payload.caption
            update_fields.append("caption")
        if payload.title is not None:
            post.title = payload.title
            update_fields.append("title")
        if payload.first_comment is not None:
            post.first_comment = payload.first_comment
            update_fields.append("first_comment")
        if payload.internal_notes is not None:
            post.internal_notes = payload.internal_notes
            update_fields.append("internal_notes")
        if payload.scheduled_at is not None:
            # Re-time any currently-scheduled child. Drafts are unaffected.
            # ``QuerySet.update()`` bypasses ``auto_now``, so we include
            # ``updated_at=timezone.now()`` explicitly. Otherwise each
            # child's ``updated_at`` would freeze at its creation time
            # despite an effective state change.
            from django.utils import timezone as _tz

            scheduled_children = post.platform_posts.filter(status="scheduled")
            scheduled_children.update(scheduled_at=payload.scheduled_at, updated_at=_tz.now())
            post.scheduled_at = payload.scheduled_at
            update_fields.append("scheduled_at")
        if payload.proposed_publish_at is not None:
            # Draft-stage suggestion; null is a no-op, consistent with the
            # other PATCH fields. Not gated on publish_directly — it never
            # reaches the publisher.
            post.proposed_publish_at = payload.proposed_publish_at
            update_fields.append("proposed_publish_at")
        if payload.media_asset_ids is not None:
            # Replace the attachment set in order. Validated above, so
            # the only remaining failure modes are DB-level — the atomic
            # block rolls back the whole route if any single
            # ``PostMedia.objects.create`` raises.
            post.media_attachments.all().delete()
            for position, mid in enumerate(wanted_media):
                PostMedia.objects.create(
                    post=post,
                    media_asset=resolved_assets[mid],
                    position=position,
                )

        if update_fields:
            post.save(update_fields=[*update_fields, "updated_at"])

        if yt_children:
            # YouTube options live in each child's ``platform_extra`` —
            # the same dict ``create_post`` writes and the publisher
            # reads at publish time. ``tags: []`` clears the stored tags;
            # every other field only writes when sent (null = no-op).
            for pp in yt_children:
                extra = dict(pp.platform_extra or {})
                if payload.privacy_status is not None:
                    extra["privacy_status"] = payload.privacy_status
                if payload.tags is not None:
                    if payload.tags:
                        extra["tags"] = list(payload.tags)
                    else:
                        extra.pop("tags", None)
                if payload.thumbnail_asset_id is not None:
                    extra["thumbnail_asset_id"] = str(payload.thumbnail_asset_id)
                if payload.category_id is not None:
                    extra["category_id"] = payload.category_id
                if payload.made_for_kids is not None:
                    extra["self_declared_made_for_kids"] = payload.made_for_kids
                pp.platform_extra = extra
                pp.save(update_fields=["platform_extra", "updated_at"])
            if not update_fields:
                # A YouTube-only PATCH changes PlatformPost rows, not the
                # Post row — still bump the post's updated_at so
                # "modified since" syncs see the change.
                from django.utils import timezone as _tz

                post.updated_at = _tz.now()
                post.save(update_fields=["updated_at"])

        sync_post_scheduled_at(post)

    post.refresh_from_db()
    log_audit_entry(request, action="post.update", target_id=post.id, status_code=200)
    return _post_to_response(request, post)


@router.post("/{post_id}/schedule", response=PostResponse, summary="Schedule a draft")
def schedule(request, post_id: uuid.UUID, payload: ScheduleRequest):
    enforce_http_rate_limits(request, is_write=True)
    # Same ``publish_directly`` contract as the create-with-schedule
    # branch: only keys that can publish-directly may push a post into
    # the SCHEDULED state. See ``create``.
    _require_perm(request, "create_posts")
    _require_perm(request, "publish_directly")
    post = _get_workspace_post(request, post_id)

    # Schedule every draft child; a single-account key produces one child,
    # but defensively we apply the transition to all draft children so we
    # don't half-schedule.
    drafts = list(post.platform_posts.filter(status="draft"))
    if not drafts:
        raise HttpError(409, "No draft platform posts to schedule.")

    # Quota check is per-account, so we evaluate it once per child before
    # we touch any state. Doing the checks first means an over-quota
    # account fails the whole route with 429 — no partial commit.
    for pp in drafts:
        check_platform_quota(pp.social_account)

    # Wrap the per-child transitions in a single outer atomic so a
    # mid-loop ValueError rolls back any earlier ``scheduled`` commits.
    # Without this, child 1 could be persisted as ``scheduled`` while
    # child 2's ``transition_to`` rejects the move (concurrent admin
    # edit, state-machine conflict) — the route 422s but the post is
    # left in a half-scheduled state.
    from django.db import transaction

    with transaction.atomic():
        for pp in drafts:
            try:
                transition_platform_post(pp, "scheduled", scheduled_at=payload.scheduled_at)
            except ValueError as exc:
                raise HttpError(422, str(exc)) from exc

    post.refresh_from_db()
    log_audit_entry(request, action="post.schedule", target_id=post.id, status_code=200)
    return _post_to_response(request, post)


@router.post("/{post_id}/cancel", response=PostResponse, summary="Cancel a scheduled post (back to draft)")
def cancel(request, post_id: uuid.UUID):
    enforce_http_rate_limits(request, is_write=True)
    _require_perm(request, "create_posts")
    post = _get_workspace_post(request, post_id)

    scheduled_children = list(post.platform_posts.filter(status="scheduled"))
    if not scheduled_children:
        raise HttpError(409, "No scheduled platform posts to cancel.")

    # Same atomic-loop reasoning as ``schedule``: any per-child transition
    # failure rolls back the whole cancellation. Half-cancelled posts
    # would otherwise leave the publisher with a mix of ``draft`` and
    # ``scheduled`` children, which is exactly the inconsistent state
    # the route was supposed to prevent.
    from django.db import transaction

    with transaction.atomic():
        for pp in scheduled_children:
            try:
                transition_platform_post(pp, "draft")
            except ValueError as exc:
                raise HttpError(422, str(exc)) from exc

    post.refresh_from_db()
    log_audit_entry(request, action="post.cancel", target_id=post.id, status_code=200)
    return _post_to_response(request, post)


@router.delete(
    "/{post_id}",
    response=PostDeleteResponse,
    summary="Delete a post",
    description=(
        "Deletes the post and all of its platform children, publish logs, "
        "media links, version snapshots and calendar queue entries. "
        "Every state may be deleted EXCEPT while a child is publishing: a "
        "post with a `publishing` child answers 422 — wait for it to "
        "finish or cancel it first. Deleting a `published` post deletes "
        "only Bean's record of it; content already live on the platform "
        "STAYS LIVE — Bean has no unpublish operation, and deleting the "
        "record does not retract the video or post. Unpublishing is not "
        "part of this API. Media assets themselves are NOT deleted (only "
        "the post's links to them); orphaned assets are swept later. A "
        "deleted post cannot be restored; a second DELETE on the same id "
        "answers 404."
    ),
)
def delete_post(request, post_id: uuid.UUID):
    enforce_http_rate_limits(request, is_write=True)
    _require_perm(request, "create_posts")
    post = _get_workspace_post(request, post_id)
    if post.platform_posts.filter(status="publishing").exists():
        raise HttpError(
            422,
            "Post is currently publishing; wait for it to finish or cancel it first.",
        )
    # Capture the id first: Django clears ``pk`` on the in-memory object
    # once the row is deleted, so reading it afterwards yields None
    # (same reason as ``live.delete``).
    row_id = post.id
    from django.db import transaction

    with transaction.atomic():
        # Cascade deletes PlatformPost → PublishLog, PostMedia (link rows
        # only — MediaAsset rows survive), PostVersion and QueueEntry.
        post.delete()
    log_audit_entry(request, action="post.delete", target_id=row_id, status_code=200)
    return PostDeleteResponse(id=row_id, deleted=True)
