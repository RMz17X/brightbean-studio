"""Chunked/resumable media uploads (v1.2.0): ``POST /api/v1/media/uploads/``.

Covers the session lifecycle end to end on the local (server-append)
backend — the backend production actually runs — plus the S3 multipart
mode with the ``apps.media_library.storage`` seam mocked (no live bucket,
following ``test_presigned_upload.py``).

Small chunk sizes are forced per-test by monkeypatching
``apps.media_library.services.UPLOAD_CHUNK_SIZE`` to 1 KiB, so a 5 KiB
file exercises 5 parts without moving megabytes through the test client.
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta

import pytest
from django.core.files.storage import default_storage
from django.test import Client
from django.utils import timezone

from apps.api.middleware import claim_idempotency_slot
from apps.api_keys import services
from apps.media_library import storage as ml_storage
from apps.media_library import tasks as ml_tasks
from apps.media_library.models import MediaAsset, MediaUploadPart, PendingUpload
from apps.media_library.quotas import StorageQuotaExceededError
from apps.media_library.services import chunked_staging_key
from apps.members.models import PERMISSION_KEYS, OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
CHUNK = 1024  # patched UPLOAD_CHUNK_SIZE per test (1 KiB parts)


class _SecureClient(Client):
    def generic(self, method, path, *args, **kwargs):
        kwargs["secure"] = True
        return super().generic(method, path, *args, **kwargs)


@pytest.fixture
def user(db):
    from apps.accounts.models import User

    return User.objects.create_user(
        email="media@example.com",
        password="x",
        name="Media",
        tos_accepted_at=timezone.now(),
    )


@pytest.fixture
def organization(db):
    return Organization.objects.create(name="Media Org")


@pytest.fixture
def workspace(db, organization):
    return Workspace.objects.create(name="Media WS", organization=organization)


@pytest.fixture
def other_workspace(db, organization):
    return Workspace.objects.create(name="Other WS", organization=organization)


@pytest.fixture
def owner_memberships(db, user, organization, workspace):
    OrgMembership.objects.create(user=user, organization=organization, org_role=OrgMembership.OrgRole.OWNER)
    return WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )


@pytest.fixture
def social_account(db, workspace):
    from apps.social_accounts.models import SocialAccount

    return SocialAccount.objects.create(
        workspace=workspace,
        platform="linkedin_personal",
        account_platform_id="li-media",
        account_name="Media LinkedIn",
        connection_status="connected",
    )


@pytest.fixture
def issued_key(db, user, owner_memberships, workspace, social_account):
    return services.issue_api_key(
        workspace=workspace,
        social_accounts=[social_account],
        issued_by=user,
        name="media",
        permissions=list(PERMISSION_KEYS),
    )


@pytest.fixture
def client_with_token(issued_key):
    return _SecureClient(HTTP_AUTHORIZATION=f"Bearer {issued_key.plaintext_token}")


@pytest.fixture
def no_upload_key(db, user, owner_memberships, workspace, social_account):
    """A key for the same workspace without the ``upload_media`` permission."""
    return services.issue_api_key(
        workspace=workspace,
        social_accounts=[social_account],
        issued_by=user,
        name="no-upload",
        permissions=["create_posts"],
    )


@pytest.fixture
def other_client(db, user, organization, other_workspace, owner_memberships):
    from apps.social_accounts.models import SocialAccount

    WorkspaceMembership.objects.create(
        user=user, workspace=other_workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    account = SocialAccount.objects.create(
        workspace=other_workspace,
        platform="linkedin_personal",
        account_platform_id="li-other",
        account_name="Other LinkedIn",
        connection_status="connected",
    )
    issued = services.issue_api_key(
        workspace=other_workspace,
        social_accounts=[account],
        issued_by=user,
        name="other",
        permissions=list(PERMISSION_KEYS),
    )
    return _SecureClient(HTTP_AUTHORIZATION=f"Bearer {issued.plaintext_token}")


@pytest.fixture
def small_chunks(monkeypatch):
    """Force 1 KiB parts so multi-part flows stay small."""
    monkeypatch.setattr("apps.media_library.services.UPLOAD_CHUNK_SIZE", CHUNK)
    return CHUNK


def _part_bytes(number: int, *, total_parts: int = 5) -> bytes:
    """Deterministic part content; part 1 carries the MP4 magic head."""
    body = bytes([number]) * CHUNK
    if number == 1:
        body = MP4 + body[len(MP4):]
    if number == total_parts:
        # Last part of a 5-part file is full-size here (total = 5 * CHUNK).
        pass
    return body


def _init(client, *, total_bytes=5 * CHUNK, media_type="video", filename="clip.mp4", **extra):
    payload = {"filename": filename, "total_bytes": total_bytes, "media_type": media_type}
    payload.update(extra)
    resp = client.post(
        "/api/v1/media/uploads/",
        data=json.dumps(payload),
        content_type="application/json",
    )
    return resp.status_code, json.loads(resp.content)


def _put_part(client, upload_id, number, data, *, total_bytes=5 * CHUNK):
    start = (number - 1) * CHUNK
    end = start + len(data) - 1
    return client.put(
        f"/api/v1/media/uploads/{upload_id}/parts/{number}",
        data=data,
        content_type="application/octet-stream",
        HTTP_CONTENT_RANGE=f"bytes {start}-{end}/{total_bytes}",
    )


def _complete(client, upload_id, **extra):
    return client.post(
        f"/api/v1/media/uploads/{upload_id}/complete",
        data=json.dumps(extra),
        content_type="application/json",
    )


def _body(resp):
    return json.loads(resp.content)


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_init_returns_session_201_local_mode(client_with_token):
    status, body = _init(client_with_token, total_bytes=5_000_000)
    assert status == 201, body
    assert body["upload_mode"] == "server_append"
    assert body["filename"] == "clip.mp4"
    assert body["total_bytes"] == 5_000_000
    assert body["chunk_size"] == 8 * 1024 * 1024
    assert body["total_parts"] == 1
    assert body["uploaded_bytes"] == 0
    assert body["received_parts"] == []
    assert body["next_part"] == 1
    assert body["media_asset_id"] is None
    pending = PendingUpload.objects.get(id=body["upload_id"])
    assert pending.s3_upload_id == ""
    # 24h session TTL (not the 900s presigned-POST TTL).
    assert pending.expires_at > timezone.now() + timedelta(hours=23)


@pytest.mark.django_db
def test_init_over_cap_422(client_with_token):
    status, body = _init(client_with_token, total_bytes=21 * 1024 * 1024, media_type="image")
    assert status == 422, body
    assert body["error"] == "unprocessable_entity"
    assert not PendingUpload.objects.exists()

    status, body = _init(client_with_token, total_bytes=1024 * 1024 * 1024 + 1, media_type="video")
    assert status == 422, body


@pytest.mark.django_db
def test_init_unknown_media_type_and_bad_content_type_422(client_with_token):
    status, body = _init(client_with_token, media_type="audio")
    assert status == 422, body

    status, body = _init(client_with_token, content_type="text/html")
    assert status == 422, body

    status, body = _init(client_with_token, total_bytes=0)
    assert status == 422, body


@pytest.mark.django_db
def test_init_foreign_folder_404(client_with_token, db):
    other_org = Organization.objects.create(name="Foreign Org")
    other_ws = Workspace.objects.create(name="Foreign WS", organization=other_org)
    from apps.media_library.models import MediaFolder

    folder = MediaFolder.objects.create(organization=other_org, workspace=other_ws, name="Nope")
    status, body = _init(client_with_token, folder_id=str(folder.id))
    assert status == 404, body
    assert not PendingUpload.objects.exists()


@pytest.mark.django_db
def test_init_without_upload_media_perm_403(no_upload_key):
    client = _SecureClient(HTTP_AUTHORIZATION=f"Bearer {no_upload_key.plaintext_token}")
    status, body = _init(client)
    assert status == 403, body


@pytest.mark.django_db
def test_init_idempotency_replay_returns_current_state(client_with_token, small_chunks):
    status, body = _init(client_with_token, idempotency_key="up-1")
    assert status == 201, body
    upload_id = body["upload_id"]

    resp = _put_part(client_with_token, upload_id, 1, _part_bytes(1))
    assert resp.status_code == 200, _body(resp)

    # Same key + same fingerprint: same session, but with the part that
    # landed since init (a stale init snapshot would show received_parts=[]).
    status, replay = _init(client_with_token, idempotency_key="up-1")
    assert status == 201, replay
    assert replay["upload_id"] == upload_id
    assert replay["received_parts"] == [1]
    assert replay["uploaded_bytes"] == CHUNK
    assert replay["next_part"] == 2
    assert PendingUpload.objects.count() == 1


@pytest.mark.django_db
def test_init_idempotency_fingerprint_mismatch_422(client_with_token):
    status, _ = _init(client_with_token, idempotency_key="up-2")
    assert status == 201
    status, body = _init(client_with_token, total_bytes=6 * CHUNK, idempotency_key="up-2")
    assert status == 422, body


@pytest.mark.django_db
def test_init_idempotency_concurrent_409(client_with_token, issued_key):
    # Hold the slot the way a concurrent init would, then init again.
    from apps.api.middleware import fingerprint_request

    fp = fingerprint_request(
        "POST",
        "/api/v1/media/uploads/",
        {
            "filename": "clip.mp4",
            "total_bytes": 5 * CHUNK,
            "media_type": "video",
            "alt_text": "",
            "title": "",
            "folder_id": None,
            "tags": [],
        },
    )
    disposition, _, _ = claim_idempotency_slot(
        api_key=issued_key.api_key, idempotency_key="up-3", fingerprint=fp
    )
    assert disposition == "claimed"
    status, body = _init(client_with_token, idempotency_key="up-3")
    assert status == 409, body


# ---------------------------------------------------------------------------
# Part URLs + PUT part (local mode)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_request_part_url_local_points_back_to_bean(client_with_token):
    status, body = _init(client_with_token)
    assert status == 201, body
    resp = client_with_token.post(
        f"/api/v1/media/uploads/{body['upload_id']}/parts",
        data=json.dumps({"part_number": 1}),
        content_type="application/json",
    )
    assert resp.status_code == 200, _body(resp)
    part = _body(resp)
    assert part["method"] == "PUT"
    assert part["upload_id"] == body["upload_id"]
    assert part["part_number"] == 1
    assert f"/api/v1/media/uploads/{body['upload_id']}/parts/1" in part["url"]


@pytest.mark.django_db
def test_request_part_out_of_range_422(client_with_token):
    status, body = _init(client_with_token)
    assert status == 201, body
    resp = client_with_token.post(
        f"/api/v1/media/uploads/{body['upload_id']}/parts",
        data=json.dumps({"part_number": 2}),
        content_type="application/json",
    )
    assert resp.status_code == 422, _body(resp)


@pytest.mark.django_db
def test_request_part_expired_session_404(client_with_token):
    status, body = _init(client_with_token)
    assert status == 201, body
    PendingUpload.objects.filter(id=body["upload_id"]).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    resp = client_with_token.post(
        f"/api/v1/media/uploads/{body['upload_id']}/parts",
        data=json.dumps({"part_number": 1}),
        content_type="application/json",
    )
    assert resp.status_code == 404, _body(resp)
    resp = client_with_token.get(f"/api/v1/media/uploads/{body['upload_id']}")
    assert resp.status_code == 404, _body(resp)


@pytest.mark.django_db
def test_put_part_wrong_range_422(client_with_token, small_chunks):
    status, body = _init(client_with_token)
    assert status == 201, body
    upload_id = body["upload_id"]

    # Range start does not match part 1's expected offset.
    resp = client_with_token.put(
        f"/api/v1/media/uploads/{upload_id}/parts/1",
        data=_part_bytes(1),
        content_type="application/octet-stream",
        HTTP_CONTENT_RANGE=f"bytes {CHUNK}-{2 * CHUNK - 1}/{5 * CHUNK}",
    )
    assert resp.status_code == 422, _body(resp)

    # Missing Content-Range entirely.
    resp = client_with_token.put(
        f"/api/v1/media/uploads/{upload_id}/parts/1",
        data=_part_bytes(1),
        content_type="application/octet-stream",
    )
    assert resp.status_code == 422, _body(resp)

    # Short body for a full part.
    resp = _put_part(client_with_token, upload_id, 1, _part_bytes(1)[:100])
    assert resp.status_code == 422, _body(resp)
    assert MediaUploadPart.objects.count() == 0


@pytest.mark.django_db
def test_get_session_resume_after_two_of_five_parts(client_with_token, small_chunks):
    status, body = _init(client_with_token)
    assert status == 201, body
    upload_id = body["upload_id"]
    for n in (1, 2):
        resp = _put_part(client_with_token, upload_id, n, _part_bytes(n))
        assert resp.status_code == 200, _body(resp)

    resp = client_with_token.get(f"/api/v1/media/uploads/{upload_id}")
    assert resp.status_code == 200, _body(resp)
    state = _body(resp)
    assert state["received_parts"] == [1, 2]
    assert state["uploaded_bytes"] == 2 * CHUNK
    assert state["next_part"] == 3


@pytest.mark.django_db
def test_session_in_other_workspace_404(client_with_token, other_client):
    status, body = _init(client_with_token)
    assert status == 201, body
    resp = other_client.get(f"/api/v1/media/uploads/{body['upload_id']}")
    assert resp.status_code == 404, _body(resp)


# ---------------------------------------------------------------------------
# Complete (local mode)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_complete_missing_parts_422(client_with_token, small_chunks):
    status, body = _init(client_with_token)
    assert status == 201, body
    upload_id = body["upload_id"]
    resp = _put_part(client_with_token, upload_id, 1, _part_bytes(1))
    assert resp.status_code == 200, _body(resp)

    resp = _complete(client_with_token, upload_id)
    assert resp.status_code == 422, _body(resp)
    assert "2" in _body(resp)["detail"] and "3" in _body(resp)["detail"]
    assert not MediaAsset.objects.exists()


@pytest.mark.django_db
def test_complete_success_local_201(client_with_token, small_chunks):
    status, body = _init(client_with_token)
    assert status == 201, body
    upload_id = body["upload_id"]
    for n in range(1, 6):
        resp = _put_part(client_with_token, upload_id, n, _part_bytes(n))
        assert resp.status_code == 200, _body(resp)

    resp = _complete(client_with_token, upload_id)
    assert resp.status_code == 201, _body(resp)
    asset_body = _body(resp)
    assert asset_body["media_type"] == "video"
    assert asset_body["mime_type"] == "video/mp4"
    assert asset_body["file_size"] == 5 * CHUNK
    assert asset_body["processing_status"] == "pending"

    pending = PendingUpload.objects.get(id=upload_id)
    assert pending.finalized_at is not None
    assert str(pending.media_asset_id) == asset_body["id"]
    # The assembled file ended up at the session's storage_key, and the
    # staging file is gone.
    assert default_storage.exists(pending.storage_key)
    assert not default_storage.exists(chunked_staging_key(pending))


@pytest.mark.django_db
def test_complete_replay_returns_same_asset(client_with_token, small_chunks):
    status, body = _init(client_with_token)
    assert status == 201, body
    upload_id = body["upload_id"]
    for n in range(1, 6):
        assert _put_part(client_with_token, upload_id, n, _part_bytes(n)).status_code == 200

    first = _complete(client_with_token, upload_id)
    assert first.status_code == 201, _body(first)
    second = _complete(client_with_token, upload_id)
    assert second.status_code == 201, _body(second)
    assert _body(second)["id"] == _body(first)["id"]
    assert MediaAsset.objects.count() == 1


@pytest.mark.django_db
def test_complete_assembled_size_mismatch_422(client_with_token, small_chunks):
    status, body = _init(client_with_token)
    assert status == 201, body
    upload_id = body["upload_id"]
    for n in range(1, 6):
        assert _put_part(client_with_token, upload_id, n, _part_bytes(n)).status_code == 200

    # All part rows are present, but the assembled file lost bytes.
    pending = PendingUpload.objects.get(id=upload_id)
    fs_path = default_storage.path(chunked_staging_key(pending))
    with open(fs_path, "r+b") as f:
        f.truncate(100)

    resp = _complete(client_with_token, upload_id)
    assert resp.status_code == 422, _body(resp)
    assert not MediaAsset.objects.exists()


@pytest.mark.django_db
def test_complete_quota_413(client_with_token, small_chunks, monkeypatch):
    status, body = _init(client_with_token)
    assert status == 201, body
    upload_id = body["upload_id"]
    for n in range(1, 6):
        assert _put_part(client_with_token, upload_id, n, _part_bytes(n)).status_code == 200

    def _exceeded(org, incoming):
        raise StorageQuotaExceededError(used=10, limit=20, attempted=incoming)

    monkeypatch.setattr("apps.media_library.quotas.enforce_storage_quota", _exceeded)
    resp = _complete(client_with_token, upload_id)
    assert resp.status_code == 413, _body(resp)
    assert _body(resp)["error"] == "storage_quota_exceeded"
    assert not MediaAsset.objects.exists()


@pytest.mark.django_db
def test_complete_queues_processing_when_pending(client_with_token, small_chunks, monkeypatch):
    """Complete enqueues process_media_asset (mocked: record, don't run)."""
    calls = []
    monkeypatch.setattr(ml_tasks, "process_media_asset", lambda *a, **k: calls.append(a[0] if a else None))
    status, body = _init(client_with_token)
    assert status == 201, body
    upload_id = body["upload_id"]
    for n in range(1, 6):
        assert _put_part(client_with_token, upload_id, n, _part_bytes(n)).status_code == 200
    resp = _complete(client_with_token, upload_id)
    assert resp.status_code == 201, _body(resp)
    assert calls == [_body(resp)["id"]]


# ---------------------------------------------------------------------------
# Abort
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_abort_session_200_and_cleans_up(client_with_token, small_chunks):
    status, body = _init(client_with_token)
    assert status == 201, body
    upload_id = body["upload_id"]
    assert _put_part(client_with_token, upload_id, 1, _part_bytes(1)).status_code == 200
    pending = PendingUpload.objects.get(id=upload_id)
    assert default_storage.exists(chunked_staging_key(pending))

    resp = client_with_token.delete(f"/api/v1/media/uploads/{upload_id}")
    assert resp.status_code == 200, _body(resp)
    assert _body(resp) == {"upload_id": upload_id, "deleted": True}
    assert not PendingUpload.objects.filter(id=upload_id).exists()
    assert not MediaUploadPart.objects.filter(pending_upload_id=upload_id).exists()
    assert not default_storage.exists(chunked_staging_key(pending))

    # Second delete: already gone.
    resp = client_with_token.delete(f"/api/v1/media/uploads/{upload_id}")
    assert resp.status_code == 404, _body(resp)


@pytest.mark.django_db
def test_abort_finalized_session_422(client_with_token, small_chunks):
    status, body = _init(client_with_token)
    assert status == 201, body
    upload_id = body["upload_id"]
    for n in range(1, 6):
        assert _put_part(client_with_token, upload_id, n, _part_bytes(n)).status_code == 200
    assert _complete(client_with_token, upload_id).status_code == 201

    resp = client_with_token.delete(f"/api/v1/media/uploads/{upload_id}")
    assert resp.status_code == 422, _body(resp)
    assert MediaAsset.objects.count() == 1


@pytest.mark.django_db
def test_request_part_after_finalize_409(client_with_token, small_chunks):
    status, body = _init(client_with_token)
    assert status == 201, body
    upload_id = body["upload_id"]
    for n in range(1, 6):
        assert _put_part(client_with_token, upload_id, n, _part_bytes(n)).status_code == 200
    done = _complete(client_with_token, upload_id)
    assert done.status_code == 201, _body(done)

    resp = client_with_token.post(
        f"/api/v1/media/uploads/{upload_id}/parts",
        data=json.dumps({"part_number": 1}),
        content_type="application/json",
    )
    assert resp.status_code == 409, _body(resp)
    assert _body(done)["id"] in _body(resp)["detail"]


# ---------------------------------------------------------------------------
# S3 multipart mode (mocked storage seam)
# ---------------------------------------------------------------------------


@pytest.fixture
def s3_seam(monkeypatch):
    """Make the service layer behave as if S3/R2 storage is configured."""
    calls: dict = {"complete": [], "abort": [], "delete": []}
    monkeypatch.setattr(ml_storage, "is_s3_backend", lambda: True)
    monkeypatch.setattr(ml_storage, "create_multipart_upload", lambda key, *, content_type: "mock-upload-id")
    monkeypatch.setattr(
        ml_storage,
        "presign_part_upload",
        lambda key, upload_id, part_number, expires_in: f"https://r2.example.invalid/part/{part_number}",
    )
    monkeypatch.setattr(
        ml_storage,
        "complete_multipart_upload",
        lambda key, upload_id, parts: calls["complete"].append((key, upload_id, parts)) or {},
    )
    monkeypatch.setattr(
        ml_storage,
        "abort_multipart_upload",
        lambda key, upload_id: calls["abort"].append((key, upload_id)),
    )
    monkeypatch.setattr(ml_storage, "head_object_size", lambda key: 5 * CHUNK)
    monkeypatch.setattr(ml_storage, "read_object_head_bytes", lambda key, n=32: MP4[:n])
    monkeypatch.setattr(ml_storage, "delete_object", lambda key: calls["delete"].append(key))
    return calls


@pytest.mark.django_db
def test_s3_init_and_part_url(client_with_token, s3_seam, small_chunks):
    status, body = _init(client_with_token)
    assert status == 201, body
    assert body["upload_mode"] == "s3_multipart"
    pending = PendingUpload.objects.get(id=body["upload_id"])
    assert pending.s3_upload_id == "mock-upload-id"

    resp = client_with_token.post(
        f"/api/v1/media/uploads/{body['upload_id']}/parts",
        data=json.dumps({"part_number": 2}),
        content_type="application/json",
    )
    assert resp.status_code == 200, _body(resp)
    assert _body(resp)["url"] == "https://r2.example.invalid/part/2"


@pytest.mark.django_db
def test_s3_put_part_rejected_422(client_with_token, s3_seam, small_chunks):
    status, body = _init(client_with_token)
    assert status == 201, body
    resp = _put_part(client_with_token, body["upload_id"], 1, _part_bytes(1))
    assert resp.status_code == 422, _body(resp)


@pytest.mark.django_db
def test_s3_complete_calls_complete_multipart(client_with_token, s3_seam, small_chunks):
    status, body = _init(client_with_token)
    assert status == 201, body
    upload_id = body["upload_id"]
    pending = PendingUpload.objects.get(id=upload_id)
    # Parts arrived via presigned PUTs straight to storage; the server only
    # recorded their ETags (simulated here by writing the rows directly).
    for n in range(1, 6):
        MediaUploadPart.objects.create(
            pending_upload=pending, part_number=n, etag=f"etag-{n}", size=CHUNK
        )

    resp = _complete(client_with_token, upload_id)
    assert resp.status_code == 201, _body(resp)
    assert _body(resp)["media_type"] == "video"
    assert len(s3_seam["complete"]) == 1
    key, upload_id_arg, parts = s3_seam["complete"][0]
    assert key == pending.storage_key
    assert upload_id_arg == "mock-upload-id"
    assert parts == [{"PartNumber": n, "ETag": f"etag-{n}"} for n in range(1, 6)]


@pytest.mark.django_db
def test_s3_abort_calls_abort_multipart(client_with_token, s3_seam, small_chunks):
    status, body = _init(client_with_token)
    assert status == 201, body
    upload_id = body["upload_id"]
    pending = PendingUpload.objects.get(id=upload_id)

    resp = client_with_token.delete(f"/api/v1/media/uploads/{upload_id}")
    assert resp.status_code == 200, _body(resp)
    assert s3_seam["abort"] == [(pending.storage_key, "mock-upload-id")]
    assert not PendingUpload.objects.filter(id=upload_id).exists()


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_sweep_aborts_expired_chunked_sessions(s3_seam, monkeypatch):
    from apps.media_library.services import init_chunked_upload

    org = Organization.objects.create(name="Sweep Org")
    ws = Workspace.objects.create(name="Sweep WS", organization=org)

    # Expired S3 session with a dangling multipart upload.
    s3_pending = init_chunked_upload(
        organization=org,
        workspace=ws,
        created_by=None,
        declared_filename="clip.mp4",
        total_bytes=5 * CHUNK,
        media_type="video",
        content_type="video/mp4",
    )
    PendingUpload.objects.filter(id=s3_pending.id).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )

    # Expired local session with a staging file on disk.
    monkeypatch.setattr(ml_storage, "is_s3_backend", lambda: False)
    local_pending = init_chunked_upload(
        organization=org,
        workspace=ws,
        created_by=None,
        declared_filename="clip.mp4",
        total_bytes=5 * CHUNK,
        media_type="video",
        content_type="video/mp4",
    )
    from apps.media_library.services import _write_staging_at_offset

    _write_staging_at_offset(local_pending, 0, b"x" * CHUNK)
    MediaUploadPart.objects.create(
        pending_upload=local_pending,
        part_number=1,
        etag=hashlib.sha256(b"x" * CHUNK).hexdigest(),
        size=CHUNK,
    )
    PendingUpload.objects.filter(id=local_pending.id).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )

    ml_tasks.sweep_pending_uploads.now()

    assert not PendingUpload.objects.filter(id=s3_pending.id).exists()
    assert not PendingUpload.objects.filter(id=local_pending.id).exists()
    assert not MediaUploadPart.objects.filter(pending_upload_id=local_pending.id).exists()
    assert s3_seam["abort"] == [(s3_pending.storage_key, "mock-upload-id")]
    assert not default_storage.exists(chunked_staging_key(local_pending))


# ---------------------------------------------------------------------------
# Backward compatibility: single-PUT still works
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_single_put_upload_still_201(client_with_token):
    from django.core.files.uploadedfile import SimpleUploadedFile

    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d49444154789c63600000000005000156a168320000000049454e44ae426082"
    )
    resp = client_with_token.post(
        "/api/v1/media/",
        data={"file": SimpleUploadedFile(name="hero.png", content=png, content_type="image/png")},
    )
    assert resp.status_code == 201, _body(resp)
    assert _body(resp)["media_type"] == "image"
