"""``/api/v1/live/*`` — YouTube Live broadcasts via the Agent API.

Thin REST layer over ``apps.social_accounts.live_service`` — the same
service the web monitor and the MCP tools use, so the three surfaces
cannot drift. One scope check, one create/bind/cleanup flow, one
transition rule, one throttled status poll, one wire shape
(``LiveBroadcastResponse``).

Stream-key rule (mirrors the web created-page): the full key is
returned exactly once, in the create / recreate-stream response. List
and detail never return it — only ``masked_stream_key``.

Idempotency (``POST /`` only, same claim-first pattern as the posts
router): the slot is claimed *before* ``live_service`` touches YouTube,
so a retried create with the same key + same body replays the stored
201 (including the same stream key — the retry is the same logical
create, whose first response the client never received) instead of
creating a second stream + broadcast on YouTube. Same key + different
body → 422; a concurrent peer holding the slot → 409. Scope is
``(api_key, key)``; records live 24h (hourly sweep in
``apps.api.tasks``). See the create route for the residual windows.

Going live on a PUBLIC broadcast needs ``{"confirm": true}`` in the
transition body (mirrors the web confirm dialog). Private/unlisted
broadcasts go live without it.
"""

from __future__ import annotations

import datetime as dt
import uuid

from django.http import Http404, HttpRequest
from django.shortcuts import get_object_or_404
from ninja import Query, Router
from ninja.errors import HttpError

from apps.api.limits import enforce_http_rate_limits
from apps.api.middleware import (
    claim_idempotency_slot,
    finalize_idempotent_response,
    fingerprint_request,
    log_audit_entry,
    release_idempotent_claim,
)
from apps.api.pagination import decode_offset_cursor, encode_offset_cursor
from apps.api.schemas import (
    LiveBroadcastCreateRequest,
    LiveBroadcastListResponse,
    LiveBroadcastResponse,
    LiveDeleteResponse,
    LiveTransitionRequest,
)
from apps.social_accounts import live_service
from apps.social_accounts.live_service import LiveServiceError
from apps.social_accounts.models import LiveBroadcast, SocialAccount

router = Router(tags=["live"])

_LIMIT_DEFAULT = 50
_LIMIT_MAX = 100

_TRANSITION_TARGETS = {"test": "testing", "go_live": "live", "stop": "complete"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_perm(request: HttpRequest, key: str) -> None:
    """Re-check a workspace permission inside a Ninja route body.

    We can't decorate Ninja routes with ``@require_permission`` because it
    expects a Django view signature; instead we inline the same check
    against the virtual membership shim (same as the posts router).
    """
    membership = getattr(request, "workspace_membership", None)
    if membership is None or not membership.effective_permissions.get(key, False):
        raise HttpError(403, f"Permission denied: {key}")


def _resolve_youtube_account(request: HttpRequest, social_account_id: uuid.UUID) -> SocialAccount:
    """Resolve the target account and verify it is in the key's allowlist.

    Defence against confused-deputy attacks: even though the bearer token is
    valid, the caller may only act on accounts the issuer explicitly
    listed at issuance time (same as the posts router).
    """
    api_key = request.api_key  # type: ignore[attr-defined]
    allowlist_ids = {sa.id for sa in api_key.social_accounts.all()}
    if social_account_id not in allowlist_ids:
        raise HttpError(403, "SocialAccount is not in this key's allowlist.")
    account = SocialAccount.objects.get(id=social_account_id)
    if account.platform != "youtube":
        raise HttpError(422, "social_account_id must reference a connected YouTube account.")
    if account.connection_status != SocialAccount.ConnectionStatus.CONNECTED:
        raise HttpError(422, "The YouTube account is not connected.")
    return account


def _get_broadcast_for_key(request: HttpRequest, broadcast_id: uuid.UUID) -> LiveBroadcast:
    """Fetch a broadcast in the key's workspace whose account is allowlisted.

    We intentionally don't distinguish "doesn't exist" from "exists in
    another workspace" from "exists but out of scope" — all three return
    404 so the API doesn't leak the existence of foreign IDs (same rule
    as the posts router's ``_get_workspace_post``).
    """
    broadcast = get_object_or_404(
        LiveBroadcast.objects.select_related("social_account"),
        id=broadcast_id,
        workspace_id=request.api_key.workspace_id,  # type: ignore[attr-defined]
    )
    allowed_ids = {sa.id for sa in request.api_key.social_accounts.all()}  # type: ignore[attr-defined]
    if broadcast.social_account_id not in allowed_ids:
        raise Http404()
    return broadcast


def _normalize_datetime(value: dt.datetime | None) -> dt.datetime | None:
    """Read a tz-less value as UTC (the documented contract).

    Same rule as the MCP ``_parse_iso_datetime`` helper, so REST and MCP
    store the same instant for the same input.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value


def _map_service_error(exc: LiveServiceError) -> HttpError:
    return HttpError(exc.status_code, exc.message)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/", response={201: LiveBroadcastResponse}, summary="Create a live broadcast")
def create_live(request, payload: LiveBroadcastCreateRequest):
    enforce_http_rate_limits(request, is_write=True)
    _require_perm(request, "create_posts")
    # Cheap validation runs BEFORE the idempotency claim (same ordering
    # as the posts router): a 403/422 here must not leave a placeholder
    # claim row behind for the caller to clean up.
    account = _resolve_youtube_account(request, payload.social_account_id)

    # ---- Atomic claim-first idempotency (posts-router pattern).
    # The claim is taken before ANY YouTube call, so it — not just the
    # local row — covers the external side effects (create_live_stream
    # + create_live_broadcast + bind): a sequential retry replays the
    # stored 201 without re-calling YouTube, and a concurrent identical
    # retry gets 409 while the first create is still in flight.
    # Body field wins over the header, same as posts/inbox.
    fingerprint = fingerprint_request(
        request.method or "POST", request.path, payload.model_dump(mode="json")
    )
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

    # Only the failure of the service call itself releases the claim:
    # on failure the service has already cleaned up the half-created
    # YouTube pair (stream and/or broadcast), so a retry can safely
    # create fresh. Once the service returns, the YouTube side effects
    # exist and the claim is NEVER released again — a later failure
    # (mark_key_shown / finalize) leaves the slot PENDING, so a retry
    # 409s instead of creating a second broadcast on YouTube.
    try:
        broadcast, stream_key = live_service.create_live_broadcast(
            request.api_key.workspace,  # type: ignore[attr-defined]
            account,
            title=payload.title,
            description=payload.description,
            privacy_status=payload.privacy_status,
            resolution=payload.resolution,
            frame_rate=payload.frame_rate,
            scheduled_start=_normalize_datetime(payload.scheduled_start),
        )
    except LiveServiceError as exc:
        release_idempotent_claim(api_key=request.api_key, idempotency_key=idempotency_key)
        raise _map_service_error(exc) from exc
    except Exception:
        release_idempotent_claim(api_key=request.api_key, idempotency_key=idempotency_key)
        raise
    # The create response discloses the key exactly once, mirroring the
    # web created-page: mark it shown before serializing so the response
    # is truthful about ``key_shown``.
    live_service.mark_key_shown(broadcast)
    body = LiveBroadcastResponse.from_broadcast(broadcast, stream_key=stream_key)
    log_audit_entry(request, action="live.create", target_id=broadcast.id, status_code=201)
    finalize_idempotent_response(
        api_key=request.api_key,
        idempotency_key=idempotency_key,
        status_code=201,
        body=body.model_dump(mode="json"),
    )
    return 201, body


@router.get("/", response=LiveBroadcastListResponse, summary="List live broadcasts")
def list_live(
    request,
    status: str | None = Query(None),
    social_account_id: uuid.UUID | None = Query(None),
    limit: int = Query(_LIMIT_DEFAULT, ge=1, le=_LIMIT_MAX),
    cursor: str | None = Query(None),
):
    enforce_http_rate_limits(request, is_write=False)
    if status is not None and status not in LiveBroadcast.Status.values:
        raise HttpError(422, f"status must be one of {', '.join(LiveBroadcast.Status.values)}")
    try:
        offset = decode_offset_cursor(cursor)
    except ValueError as exc:
        raise HttpError(422, "cursor is not a valid pagination cursor") from exc

    allowed_ids = {sa.id for sa in request.api_key.social_accounts.all()}  # type: ignore[attr-defined]
    qs = (
        LiveBroadcast.objects.for_workspace(request.api_key.workspace_id)  # type: ignore[attr-defined]
        .filter(social_account_id__in=allowed_ids)
        .select_related("social_account")
    )
    if status:
        qs = qs.filter(status=status)
    if social_account_id is not None:
        if social_account_id not in allowed_ids:
            raise HttpError(403, "social_account_id is not in this key's allowlist.")
        qs = qs.filter(social_account_id=social_account_id)
    qs = qs.order_by("-created_at", "id")

    rows = list(qs[offset : offset + limit + 1])
    has_more = len(rows) > limit
    rows = rows[:limit]
    log_audit_entry(request, action="live.list", target_id=None, status_code=200)
    return LiveBroadcastListResponse(
        broadcasts=[LiveBroadcastResponse.from_broadcast(b) for b in rows],
        limit=limit,
        next_cursor=encode_offset_cursor(offset + limit) if has_more else None,
    )


@router.get("/{broadcast_id}", response=LiveBroadcastResponse, summary="Read one live broadcast")
def retrieve_live(request, broadcast_id: uuid.UUID):
    enforce_http_rate_limits(request, is_write=False)
    broadcast = _get_broadcast_for_key(request, broadcast_id)
    if broadcast.is_active:
        # Throttled inside the service (45 s) — a detail read never
        # spends more than one quota unit per interval.
        live_service.refresh_broadcast_status(broadcast)
        broadcast.refresh_from_db()
    log_audit_entry(request, action="live.read", target_id=broadcast.id, status_code=200)
    return LiveBroadcastResponse.from_broadcast(broadcast)


@router.post(
    "/{broadcast_id}/transition",
    response=LiveBroadcastResponse,
    summary="Test / go live / stop a broadcast",
)
def transition_live(request, broadcast_id: uuid.UUID, payload: LiveTransitionRequest):
    enforce_http_rate_limits(request, is_write=True)
    _require_perm(request, "create_posts")
    broadcast = _get_broadcast_for_key(request, broadcast_id)
    target = _TRANSITION_TARGETS[payload.action]
    if target == "live" and broadcast.privacy_status == "public" and not payload.confirm:
        raise HttpError(
            400,
            'This broadcast is PUBLIC. Pass {"confirm": true} to go live — viewers will be able to watch immediately.',
        )
    try:
        live_service.transition_live_broadcast(broadcast, target)
    except LiveServiceError as exc:
        raise _map_service_error(exc) from exc
    log_audit_entry(request, action="live.transition", target_id=broadcast.id, status_code=200)
    return LiveBroadcastResponse.from_broadcast(broadcast)


@router.post(
    "/{broadcast_id}/recreate-stream",
    response=LiveBroadcastResponse,
    summary="Replace the ingest stream (new stream key)",
)
def recreate_stream(request, broadcast_id: uuid.UUID):
    enforce_http_rate_limits(request, is_write=True)
    _require_perm(request, "create_posts")
    broadcast = _get_broadcast_for_key(request, broadcast_id)
    try:
        broadcast, stream_key = live_service.recreate_live_stream(broadcast)
    except LiveServiceError as exc:
        raise _map_service_error(exc) from exc
    # Same once-only disclosure as create.
    live_service.mark_key_shown(broadcast)
    body = LiveBroadcastResponse.from_broadcast(broadcast, stream_key=stream_key)
    log_audit_entry(request, action="live.recreate_stream", target_id=broadcast.id, status_code=200)
    return body


@router.delete("/{broadcast_id}", response=LiveDeleteResponse, summary="Delete a live broadcast")
def delete_live(request, broadcast_id: uuid.UUID):
    enforce_http_rate_limits(request, is_write=True)
    _require_perm(request, "create_posts")
    broadcast = _get_broadcast_for_key(request, broadcast_id)
    # Capture the ids first: Django clears ``pk`` on the in-memory object
    # once the row is deleted, so reading them afterwards yields None.
    row_id, yt_broadcast_id = broadcast.id, broadcast.broadcast_id
    live_service.delete_live_broadcast(broadcast)
    log_audit_entry(request, action="live.delete", target_id=row_id, status_code=200)
    return LiveDeleteResponse(id=row_id, broadcast_id=yt_broadcast_id, deleted=True)
