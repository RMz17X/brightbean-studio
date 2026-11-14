"""``/api/v1/media/*`` — upload, list, retrieve media assets (Gap 1 + 1b).

Upload (POST /) is the only way an agent can introduce a binary into a
workspace; every other write endpoint references already-uploaded
``MediaAsset`` UUIDs. Both this router and the cookie-authenticated UI
flow through ``apps.media_library.services.create_asset``, which is the
single chokepoint for MIME sniffing, size validation, and storage-quota
enforcement (Gap 1a).

The list endpoint is built around the *reuse* scenario: the most useful
filters are recency (``-created_at`` default), ``last_used_at``
(annotated, not stored — see ``MediaAssetManager.with_last_used_at``),
and ``processing_status=completed`` so agents never reference an asset
that hasn't finished async processing.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from ninja import File, Form, Query, Router
from ninja.errors import HttpError
from ninja.files import UploadedFile

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
    MediaAssetListResponse,
    MediaAssetResponse,
    UploadChunkResponse,
    UploadPartRequest,
    UploadPartResponse,
    UploadSessionAbortResponse,
    UploadSessionCompleteRequest,
    UploadSessionRequest,
    UploadSessionResponse,
)
from apps.media_library.models import MediaAsset, PendingUpload
from apps.media_library.services import (
    ChunkedUploadIncompleteError,
    abort_chunked_upload,
    complete_chunked_upload,
    create_asset,
    init_chunked_upload,
    store_local_chunk,
    upload_session_state,
)

router = Router(tags=["media"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_perm(request: HttpRequest, key: str) -> None:
    membership = getattr(request, "workspace_membership", None)
    if membership is None or not membership.effective_permissions.get(key, False):
        raise HttpError(403, f"Permission denied: {key}")


def _visible_assets_qs(request: HttpRequest):
    """Workspace-scoped assets plus org-shared (workspace_id IS NULL)."""
    workspace = request.workspace  # type: ignore[attr-defined]  # set by ApiKeyAuth
    return MediaAsset.objects.for_workspace_with_shared(
        workspace_id=workspace.id,
        organization_id=workspace.organization_id,
    )


def _parse_tags_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [t.strip() for t in value.split(",") if t.strip()]


# ---------------------------------------------------------------------------
# POST /api/v1/media/  (Gap 1)
# ---------------------------------------------------------------------------


@router.post(
    "/",
    response={201: MediaAssetResponse},
    summary="Upload a new media asset (multipart/form-data)",
    description=(
        "Idempotency: send `idempotency_key` as a form field, or the "
        "`Idempotency-Key` header — the form field wins when both are sent. "
        "Same key + same upload (filename, size, and form fields — the file "
        "bytes themselves are not hashed) → the first 201 is replayed; same "
        "key + different upload → 422; a concurrent upload holding the same "
        "key → 409. Keys live 24h and are scoped to this API key."
    ),
)
def upload(
    request,
    file: UploadedFile = File(...),
    alt_text: str = Form(""),
    title: str = Form(""),
    folder_id: uuid.UUID | None = Form(None),
    tags: str = Form(""),
    idempotency_key: str | None = Form(
        None,
        description=(
            "Optional client-chosen retry key. Wins over the `Idempotency-Key` "
            "header when both are sent. Same key + same upload → replay; same "
            "key + different upload → 422; concurrent holder → 409."
        ),
    ),
):
    """Upload a binary into the workspace's media library.

    Body is ``multipart/form-data`` because agents posting videos can't
    cleanly base64-encode 100 MB into JSON. MCP gets a separate
    ``upload_media`` tool that accepts base64 for small files (≤5 MB).

    ``alt_text`` / ``title`` / ``folder_id`` / ``tags`` are optional and
    persisted alongside the asset, saving the agent a follow-up PATCH.
    ``tags`` is a CSV ("hero,launch") because multipart can't carry JSON
    arrays cleanly.
    """
    enforce_http_rate_limits(request, is_write=True)
    _require_perm(request, "upload_media")

    # Idempotency: hash the relevant params (NOT the file body — too
    # large, and a retry sending the same key with the same file is the
    # common case anyway).
    fp_payload = {
        "filename": file.name,
        "size": file.size,
        "alt_text": alt_text,
        "title": title,
        "folder_id": str(folder_id) if folder_id else None,
        "tags": tags,
    }
    fingerprint = fingerprint_request(request.method or "POST", request.path, fp_payload)
    effective_key = idempotency_key or request.headers.get("Idempotency-Key") or None
    try:
        disposition, replay_status, replay_body = claim_idempotency_slot(
            api_key=request.api_key,
            idempotency_key=effective_key,
            fingerprint=fingerprint,
        )
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    if disposition == "replay":
        return replay_status, replay_body
    if disposition == "in_flight":
        raise HttpError(
            409,
            "An identical upload with this idempotency_key is still in flight; retry shortly.",
        )

    workspace = request.workspace
    folder = None
    if folder_id is not None:
        from apps.media_library.models import MediaFolder

        folder = get_object_or_404(
            MediaFolder.objects.filter(
                organization=workspace.organization,
            ),
            id=folder_id,
        )

    try:
        asset = create_asset(
            organization=workspace.organization,
            workspace=workspace,
            uploaded_file=file,
            uploaded_by=request.user if not request.user.is_anonymous else None,
            folder=folder,
            alt_text=alt_text,
            title=title,
            tags=_parse_tags_csv(tags),
        )
    except ValidationError as exc:
        release_idempotent_claim(api_key=request.api_key, idempotency_key=effective_key)
        raise HttpError(422, _flatten_validation_error(exc)) from exc
    except Exception:
        release_idempotent_claim(api_key=request.api_key, idempotency_key=effective_key)
        raise

    # Mirror the cookie-auth UI's behavior: queue async processing for
    # thumbnails, dimensions, duration, and platform-specific variants.
    # See [apps/media_library/views.py:162]. Without this, processing_status
    # stays at ``pending`` forever and dependent fields stay at their zero
    # defaults.
    from apps.media_library.tasks import process_media_asset

    process_media_asset(str(asset.id))

    body = MediaAssetResponse.from_asset(asset)
    status_code = 201
    log_audit_entry(
        request,
        action=f"media.upload.{status_code}",
        target_id=asset.id,
        status_code=status_code,
    )
    finalize_idempotent_response(
        api_key=request.api_key,
        idempotency_key=effective_key,
        status_code=status_code,
        body=body.model_dump(mode="json"),
    )
    return status_code, body


def _flatten_validation_error(exc: ValidationError) -> str:
    """Render Django's nested ValidationError as a single readable line."""
    if hasattr(exc, "message_dict"):
        flat: list[str] = []
        for field, errors in exc.message_dict.items():
            joined = "; ".join(str(e) for e in errors)
            flat.append(f"{field}: {joined}")
        return " | ".join(flat)
    if hasattr(exc, "messages"):
        return "; ".join(exc.messages)
    return str(exc)


# ---------------------------------------------------------------------------
# Chunked / resumable uploads (v1.2.0)
# ---------------------------------------------------------------------------
#
# POST /uploads/ (init) → POST /uploads/{id}/parts (per-part PUT URL) →
# PUT bytes (straight to S3, or to Bean's PUT part endpoint on local
# backends) → POST /uploads/{id}/complete → 201 MediaAssetResponse.
# GET /uploads/{id} is the resume point; DELETE /uploads/{id} aborts.
# The single-PUT POST / above is untouched (backward compatible).


def _get_workspace_session(request: HttpRequest, upload_id: uuid.UUID) -> PendingUpload:
    """Tenant-scoped session fetch: foreign/other-workspace IDs read as 404."""
    try:
        return PendingUpload.objects.get(id=upload_id, workspace_id=request.workspace.id)
    except PendingUpload.DoesNotExist as exc:
        raise HttpError(404, "Upload session not found.") from exc


def _require_live_session(pending: PendingUpload) -> None:
    """404 for an expired, never-finalized session (finalized ones stay readable)."""
    from django.utils import timezone

    if not pending.finalized_at and pending.expires_at < timezone.now():
        raise HttpError(404, "Upload session not found.")


def _require_not_finalized(pending: PendingUpload) -> None:
    if pending.finalized_at:
        raise HttpError(
            409,
            "Upload session already finalized."
            + (f" media_asset_id={pending.media_asset_id}." if pending.media_asset_id else ""),
        )


def _resolve_session_folder(request: HttpRequest, folder_id: uuid.UUID | None):
    """Folder lookup scoped to the workspace's org; foreign IDs read as 404."""
    if folder_id is None:
        return None
    from apps.media_library.models import MediaFolder

    return get_object_or_404(
        MediaFolder.objects.filter(organization=request.workspace.organization),
        id=folder_id,
    )


@router.post(
    "/uploads/",
    response={201: UploadSessionResponse},
    summary="Start a chunked upload session",
    description=(
        "Idempotency: send `idempotency_key` in the body, or the "
        "`Idempotency-Key` header — the body wins when both are sent. Same "
        "key + same upload (filename, total_bytes, media_type, alt_text, "
        "title, folder_id, tags) → the CURRENT session state is replayed "
        "(same upload_id, latest received parts); same key + different "
        "upload → 422; a concurrent init holding the same key → 409. Keys "
        "live 24h and are scoped to this API key. Sessions live 24h; parts "
        "are 8 MiB each (server-chosen). The size cap is per media_type "
        "(video 1 GB, image/gif/document 20 MB) and is checked up front."
    ),
)
def init_upload_session(request, payload: UploadSessionRequest):
    enforce_http_rate_limits(request, is_write=True)
    _require_perm(request, "upload_media")

    # Cheap validation BEFORE claiming the idempotency slot, so a rejected
    # init never leaves a phantom in-flight claim behind.
    _resolve_session_folder(request, payload.folder_id)

    fp_payload = {
        "filename": payload.filename,
        "total_bytes": payload.total_bytes,
        "media_type": payload.media_type,
        "alt_text": payload.alt_text,
        "title": payload.title,
        "folder_id": str(payload.folder_id) if payload.folder_id else None,
        "tags": list(payload.tags or []),
    }
    fingerprint = fingerprint_request(request.method or "POST", request.path, fp_payload)
    effective_key = payload.idempotency_key or request.headers.get("Idempotency-Key") or None
    try:
        disposition, replay_status, replay_body = claim_idempotency_slot(
            api_key=request.api_key,
            idempotency_key=effective_key,
            fingerprint=fingerprint,
        )
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    if disposition == "replay":
        # A session is mutable state: the stored init body is stale the
        # moment a part lands. Replay the CURRENT state instead — same
        # upload_id, latest received parts — loaded via the stored body.
        try:
            pending = _get_workspace_session(request, uuid.UUID(str((replay_body or {}).get("upload_id"))))
        except (ValueError, AttributeError, TypeError):
            return replay_status, replay_body
        return 201, UploadSessionResponse(**upload_session_state(pending))
    if disposition == "in_flight":
        raise HttpError(
            409,
            "An identical upload init with this idempotency_key is still in flight; retry shortly.",
        )

    try:
        pending = init_chunked_upload(
            organization=request.workspace.organization,
            workspace=request.workspace,
            created_by=request.user if not request.user.is_anonymous else None,
            declared_filename=payload.filename,
            total_bytes=payload.total_bytes,
            media_type=payload.media_type,
            content_type=payload.content_type or "",
        )
    except ValidationError as exc:
        release_idempotent_claim(api_key=request.api_key, idempotency_key=effective_key)
        raise HttpError(422, _flatten_validation_error(exc)) from exc
    except Exception:
        release_idempotent_claim(api_key=request.api_key, idempotency_key=effective_key)
        raise

    body = UploadSessionResponse(**upload_session_state(pending))
    status_code = 201
    log_audit_entry(
        request,
        action=f"media.upload_session.init.{status_code}",
        target_id=pending.id,
        status_code=status_code,
    )
    finalize_idempotent_response(
        api_key=request.api_key,
        idempotency_key=effective_key,
        status_code=status_code,
        body=body.model_dump(mode="json"),
    )
    return status_code, body


@router.post(
    "/uploads/{upload_id}/parts",
    response={200: UploadPartResponse},
    summary="Get the PUT URL for one part",
    description=(
        "S3 mode: a presigned PUT URL for the part (TTL 900s — ask again "
        "when it expires; asking twice for the same part is safe). Local "
        "mode: the URL points back at Bean's PUT part endpoint below. "
        "Part numbers outside 1..total_parts → 422."
    ),
)
def request_part_url(request, upload_id: uuid.UUID, payload: UploadPartRequest):
    from datetime import timedelta

    from django.utils import timezone

    from apps.media_library.storage import presign_part_upload

    enforce_http_rate_limits(request, is_write=True)
    _require_perm(request, "upload_media")

    pending = _get_workspace_session(request, upload_id)
    _require_live_session(pending)
    _require_not_finalized(pending)

    if payload.part_number < 1 or payload.part_number > pending.total_parts:
        raise HttpError(422, f"part_number must be between 1 and {pending.total_parts}.")

    expires_in = int(getattr(settings, "MEDIA_LIBRARY_PRESIGN_EXPIRE", 900))
    expires_at = timezone.now() + timedelta(seconds=expires_in)
    if pending.upload_mode == PendingUpload.UploadMode.S3_MULTIPART:
        url = presign_part_upload(
            pending.storage_key,
            pending.s3_upload_id,
            payload.part_number,
            expires_in=expires_in,
        )
    else:
        url = request.build_absolute_uri(f"/api/v1/media/uploads/{pending.id}/parts/{payload.part_number}")

    body = UploadPartResponse(
        upload_id=pending.id,
        part_number=payload.part_number,
        method="PUT",
        url=url,
        expires_at=expires_at,
    )
    log_audit_entry(
        request,
        action="media.upload_session.part_url.200",
        target_id=pending.id,
        status_code=200,
    )
    return 200, body


@router.put(
    "/uploads/{upload_id}/parts/{part_number}",
    response={200: UploadChunkResponse},
    summary="Upload one part (local/server-append mode only)",
    description=(
        "Only for `server_append` sessions — S3 sessions PUT straight to "
        "the presigned URL. `Content-Range: bytes <start>-<end>/<total>` "
        "is mandatory and must match the part's expected offset and the "
        "declared total; out-of-order ranges and wrong-sized parts → 422. "
        "Re-PUTting a part rewrites the same offset (idempotent)."
    ),
)
def upload_part(request, upload_id: uuid.UUID, part_number: int):
    enforce_http_rate_limits(request, is_write=True)
    _require_perm(request, "upload_media")

    pending = _get_workspace_session(request, upload_id)
    _require_live_session(pending)
    _require_not_finalized(pending)

    if pending.upload_mode != PendingUpload.UploadMode.SERVER_APPEND:
        raise HttpError(
            422,
            "This upload session does not accept direct part uploads; "
            "upload each part to its presigned URL instead.",
        )
    if part_number < 1 or part_number > pending.total_parts:
        raise HttpError(422, f"part_number must be between 1 and {pending.total_parts}.")

    try:
        result = store_local_chunk(
            pending=pending,
            part_number=part_number,
            data=request.body or b"",
            content_range=request.headers.get("Content-Range"),
        )
    except ValidationError as exc:
        raise HttpError(422, _flatten_validation_error(exc)) from exc

    body = UploadChunkResponse(**result)
    log_audit_entry(
        request,
        action="media.upload_session.part_put.200",
        target_id=pending.id,
        status_code=200,
    )
    return 200, body


@router.get(
    "/uploads/{upload_id}",
    response={200: UploadSessionResponse},
    summary="Get upload session status (resume point)",
)
def get_upload_session(request, upload_id: uuid.UUID):
    enforce_http_rate_limits(request, is_write=False)
    _require_perm(request, "upload_media")

    pending = _get_workspace_session(request, upload_id)
    _require_live_session(pending)
    body = UploadSessionResponse(**upload_session_state(pending))
    log_audit_entry(
        request,
        action="media.upload_session.read.200",
        target_id=pending.id,
        status_code=200,
    )
    return 200, body


@router.post(
    "/uploads/{upload_id}/complete",
    response={201: MediaAssetResponse},
    summary="Complete a chunked upload session",
    description=(
        "Idempotent: completing an already-finalized session returns the "
        "same asset (201). Missing parts → 422 with the missing part "
        "numbers; assembled size ≠ declared total → 422; quota → 413. "
        "Optional body carries the asset metadata (folder_id, alt_text, "
        "title, tags) applied at finalize time."
    ),
)
def complete_upload_session(request, upload_id: uuid.UUID, payload: UploadSessionCompleteRequest | None = None):
    enforce_http_rate_limits(request, is_write=True)
    _require_perm(request, "upload_media")

    pending = _get_workspace_session(request, upload_id)
    _require_live_session(pending)
    if payload is not None:
        folder = _resolve_session_folder(request, payload.folder_id)
    else:
        folder = None
        payload = UploadSessionCompleteRequest()

    try:
        asset = complete_chunked_upload(
            pending=pending,
            uploaded_by=request.user if not request.user.is_anonymous else None,
            folder=folder,
            alt_text=payload.alt_text or "",
            title=payload.title or "",
            tags=list(payload.tags or []),
        )
    except FileNotFoundError as exc:
        raise HttpError(422, str(exc)) from exc
    except ChunkedUploadIncompleteError as exc:
        raise HttpError(422, f"Upload is incomplete; missing parts: {', '.join(str(n) for n in exc.missing)}.") from exc
    except ValidationError as exc:
        raise HttpError(422, _flatten_validation_error(exc)) from exc

    from apps.media_library.tasks import process_media_asset

    if asset.processing_status == MediaAsset.ProcessingStatus.PENDING:
        process_media_asset(str(asset.id))

    body = MediaAssetResponse.from_asset(asset)
    status_code = 201
    log_audit_entry(
        request,
        action=f"media.upload_session.complete.{status_code}",
        target_id=asset.id,
        status_code=status_code,
    )
    return status_code, body


@router.delete(
    "/uploads/{upload_id}",
    response={200: UploadSessionAbortResponse},
    summary="Abort a chunked upload session",
    description=(
        "Aborts the S3 multipart (local mode: deletes the staging file) "
        "and removes the session and its part rows. An already-finalized "
        "session → 422; unknown session → 404."
    ),
)
def abort_upload_session(request, upload_id: uuid.UUID):
    enforce_http_rate_limits(request, is_write=True)
    _require_perm(request, "upload_media")

    pending = _get_workspace_session(request, upload_id)
    _require_live_session(pending)
    try:
        abort_chunked_upload(pending)
    except ValidationError as exc:
        raise HttpError(422, _flatten_validation_error(exc)) from exc

    body = UploadSessionAbortResponse(upload_id=upload_id, deleted=True)
    log_audit_entry(
        request,
        action="media.upload_session.abort.200",
        target_id=upload_id,
        status_code=200,
    )
    return 200, body


# ---------------------------------------------------------------------------
# GET /api/v1/media/{id}  (Gap 1 + 1b)
# ---------------------------------------------------------------------------


@router.get("/{media_id}", response=MediaAssetResponse, summary="Retrieve a single media asset")
def retrieve(request, media_id: uuid.UUID):
    enforce_http_rate_limits(request, is_write=False)
    qs = MediaAsset.objects.with_last_used_at(_visible_assets_qs(request))
    asset = get_object_or_404(qs, id=media_id)
    log_audit_entry(request, action="media.read.200", target_id=asset.id, status_code=200)
    return MediaAssetResponse.from_asset(asset, last_used_at=getattr(asset, "last_used_at", None))


# ---------------------------------------------------------------------------
# GET /api/v1/media/  (Gap 1b)
# ---------------------------------------------------------------------------


_ORDER_BY_WHITELIST = {
    "-created_at",
    "created_at",
    "-updated_at",
    "updated_at",
    "-last_used_at",
    "last_used_at",
}

_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100


def _decode_cursor(cursor: str | None) -> int:
    """REST-facing wrapper: a bad cursor is a 422, not a 500."""
    try:
        return decode_offset_cursor(cursor)
    except ValueError as exc:
        raise HttpError(422, "Invalid cursor.") from exc


@router.get(
    "/",
    response=MediaAssetListResponse,
    summary="List media assets — recency-first, with filters for the reuse case",
)
def list_media(
    request,
    q: str | None = Query(None, description="Substring search on filename + tags."),
    media_type: str | None = Query(None, description="image | video | gif | document"),
    tags: str | None = Query(None, description="Comma-separated tags; ALL must match."),
    folder_id: uuid.UUID | None = Query(None),
    is_starred: bool | None = Query(None),
    processing_status: str = Query(
        "completed",
        description=(
            "Default ``completed`` so agents never reference half-processed assets. "
            "Set to ``any`` to include in-flight uploads."
        ),
    ),
    created_after: str | None = Query(None),
    created_before: str | None = Query(None),
    order_by: str = Query("-created_at"),
    cursor: str | None = Query(None),
    limit: int = Query(_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
):
    enforce_http_rate_limits(request, is_write=False)

    if order_by not in _ORDER_BY_WHITELIST:
        raise HttpError(422, f"order_by must be one of {sorted(_ORDER_BY_WHITELIST)}.")

    qs = MediaAsset.objects.with_last_used_at(_visible_assets_qs(request))

    if processing_status and processing_status.lower() != "any":
        qs = qs.filter(processing_status=processing_status)
    if media_type:
        qs = qs.filter(media_type=media_type)
    if folder_id is not None:
        qs = qs.filter(folder_id=folder_id)
    if is_starred is not None:
        qs = qs.filter(is_starred=is_starred)
    for tag in _parse_tags_csv(tags):
        qs = qs.filter(tags__contains=[tag])
    if created_after:
        qs = qs.filter(created_at__gte=created_after)
    if created_before:
        qs = qs.filter(created_at__lte=created_before)
    if q:
        qs = MediaAsset.objects.search(q, queryset=qs)

    qs = qs.order_by(order_by, "id")

    # Simple offset cursor (see ``apps.api.pagination``) — correct here because
    # the ``order_by(..., "id")`` tiebreak above makes the ordering stable.
    offset = _decode_cursor(cursor)
    items_qs = qs[offset : offset + limit + 1]
    rows = list(items_qs)
    has_more = len(rows) > limit
    rows = rows[:limit]

    body = MediaAssetListResponse(
        items=[MediaAssetResponse.from_asset(a, last_used_at=getattr(a, "last_used_at", None)) for a in rows],
        next_cursor=encode_offset_cursor(offset + limit) if has_more else None,
        limit=limit,
    )
    log_audit_entry(request, action="media.read.200", target_id=None, status_code=200)
    return body
