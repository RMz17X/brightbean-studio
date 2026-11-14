"""Service-level tests for the presigned direct-to-storage upload path.

The S3/R2 object operations live behind ``apps.media_library.storage``; here we
monkeypatch that seam so the validation chokepoint (``inspect_uploaded_object``)
and the DB step (``register_uploaded_asset``) can be exercised without a live
bucket. Both import the seam lazily, so patching the module attribute is picked
up at call time.
"""

from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.utils import timezone

from apps.media_library import storage as ml_storage
from apps.media_library.models import MediaAsset, PendingUpload
from apps.media_library.quotas import StorageQuotaExceededError
from apps.media_library.services import inspect_uploaded_object, register_uploaded_asset
from apps.media_library.validators import MAX_FILE_SIZES
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

# Magic-byte heads the sniffer recognises (first 32 bytes are enough).
MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
NOT_MEDIA = b"<html><body>not a media file</body></html>"


def _make_pending(*, filename="clip.mp4"):
    org = Organization.objects.create(name="Org")
    ws = Workspace.objects.create(organization=org, name="WS")
    return PendingUpload.objects.create(
        organization=org,
        workspace=ws,
        created_by=None,
        storage_key=ml_storage.generate_storage_key(filename),
        declared_content_type="video/mp4",
        declared_filename=filename,
        max_bytes=MAX_FILE_SIZES["video"],
        expires_at=timezone.now() + timedelta(seconds=900),
    )


def test_generate_storage_key_is_safe_against_traversal():
    # Filename never reaches the path: a UUID basename, no traversal, no
    # disallowed extension leaks through.
    traversal = ml_storage.generate_storage_key("../../etc/passwd")
    assert traversal.startswith("media_library/")
    assert ".." not in traversal

    double_ext = ml_storage.generate_storage_key("evil.mp4.exe")
    assert not double_ext.endswith(".exe")

    # An allowlisted extension is preserved (cosmetic only).
    assert ml_storage.generate_storage_key("clip.mp4").endswith(".mp4")


def test_normalize_uses_existing_backend_api(monkeypatch):
    # Regression guard: _normalize must NOT call default_storage._clean_name,
    # which was removed in django-storages 1.14 (cleaning is now the module-level
    # storages.utils.clean_name). The fake mirrors the real S3 backend: it has
    # _normalize_name but no _clean_name, so a regression raises AttributeError.
    class _FakeS3Backend:
        def _normalize_name(self, name):
            return f"prefix/{name}"

    monkeypatch.setattr(ml_storage, "default_storage", _FakeS3Backend())
    assert ml_storage._normalize("media_library/2026/06/abc.mp4") == "prefix/media_library/2026/06/abc.mp4"


@pytest.mark.django_db
def test_inspect_then_register_happy_path(monkeypatch):
    pending = _make_pending()
    size = 5_000_000
    monkeypatch.setattr(ml_storage, "head_object_size", lambda key: size)
    monkeypatch.setattr(ml_storage, "read_object_head_bytes", lambda key, n=32: MP4[:n])

    inspected = inspect_uploaded_object(pending)
    assert inspected == {"size": size, "media_type": "video", "mime": "video/mp4"}

    asset = register_uploaded_asset(pending=pending, inspected=inspected, uploaded_by=None)
    assert asset.media_type == MediaAsset.MediaType.VIDEO
    assert asset.mime_type == "video/mp4"
    assert asset.file_size == size
    assert asset.processing_status == MediaAsset.ProcessingStatus.PENDING
    # The FileField points at the existing key …
    assert asset.file.name == pending.storage_key
    # … and no bytes were re-uploaded (the key never materialised locally).
    assert not default_storage.exists(pending.storage_key)


@pytest.mark.django_db
def test_inspect_spoofed_content_rejected_and_object_deleted(monkeypatch):
    pending = _make_pending()
    deleted = []
    monkeypatch.setattr(ml_storage, "head_object_size", lambda key: 1234)
    monkeypatch.setattr(ml_storage, "read_object_head_bytes", lambda key, n=32: NOT_MEDIA[:n])
    monkeypatch.setattr(ml_storage, "delete_object", lambda key: deleted.append(key))

    with pytest.raises(ValidationError):
        inspect_uploaded_object(pending)

    assert deleted == [pending.storage_key]
    assert not MediaAsset.objects.exists()


@pytest.mark.django_db
def test_inspect_oversize_rejected_and_object_deleted(monkeypatch):
    pending = _make_pending()
    deleted = []
    monkeypatch.setattr(ml_storage, "head_object_size", lambda key: MAX_FILE_SIZES["video"] + 1)
    monkeypatch.setattr(ml_storage, "read_object_head_bytes", lambda key, n=32: MP4[:n])
    monkeypatch.setattr(ml_storage, "delete_object", lambda key: deleted.append(key))

    with pytest.raises(ValidationError):
        inspect_uploaded_object(pending)

    assert deleted == [pending.storage_key]


@pytest.mark.django_db
def test_inspect_quota_exceeded_rejected_and_object_deleted(monkeypatch):
    pending = _make_pending()
    deleted = []
    monkeypatch.setattr(ml_storage, "head_object_size", lambda key: 5_000_000)
    monkeypatch.setattr(ml_storage, "read_object_head_bytes", lambda key, n=32: MP4[:n])
    monkeypatch.setattr(ml_storage, "delete_object", lambda key: deleted.append(key))

    def _exceeded(org, incoming):
        raise StorageQuotaExceededError(used=10, limit=20, attempted=incoming)

    monkeypatch.setattr("apps.media_library.quotas.enforce_storage_quota", _exceeded)

    with pytest.raises(StorageQuotaExceededError):
        inspect_uploaded_object(pending)

    assert deleted == [pending.storage_key]


@pytest.mark.django_db
def test_inspect_delete_failure_does_not_mask_original_error(monkeypatch):
    # On a rejection the cleanup delete is best-effort; a storage error there
    # must NOT replace the real ValidationError the handler maps to a clean message.
    pending = _make_pending()
    monkeypatch.setattr(ml_storage, "head_object_size", lambda key: 1234)
    monkeypatch.setattr(ml_storage, "read_object_head_bytes", lambda key, n=32: NOT_MEDIA[:n])

    def _boom(key):
        raise RuntimeError("R2 unavailable")

    monkeypatch.setattr(ml_storage, "delete_object", _boom)

    with pytest.raises(ValidationError):
        inspect_uploaded_object(pending)


@pytest.mark.django_db
def test_inspect_missing_or_empty_object_raises(monkeypatch):
    pending = _make_pending()

    # Missing object (HEAD 404 → None): the agent never completed the upload.
    monkeypatch.setattr(ml_storage, "head_object_size", lambda key: None)
    with pytest.raises(FileNotFoundError):
        inspect_uploaded_object(pending)

    # 0-byte object: treated as incomplete, and guards against a 416 range-GET.
    monkeypatch.setattr(ml_storage, "head_object_size", lambda key: 0)
    with pytest.raises(FileNotFoundError):
        inspect_uploaded_object(pending)

    assert not MediaAsset.objects.exists()


# ---------------------------------------------------------------------------
# Chunked uploads (v1.2.0): S3 multipart seam + local HEAD/read branches
# ---------------------------------------------------------------------------


class _FakeS3Client:
    """Minimal boto3 client double recording multipart calls."""

    def __init__(self):
        self.calls = []

    def create_multipart_upload(self, *, Bucket, Key, ContentType):
        self.calls.append(("create_multipart_upload", Bucket, Key, ContentType))
        return {"UploadId": "mock-upload-id"}

    def generate_presigned_url(self, operation, *, Params, ExpiresIn):
        self.calls.append(("generate_presigned_url", operation, Params, ExpiresIn))
        return f"https://r2.example.invalid/{Params['PartNumber']}?sig=x"

    def complete_multipart_upload(self, *, Bucket, Key, UploadId, MultipartUpload):
        self.calls.append(("complete_multipart_upload", Bucket, Key, UploadId, MultipartUpload))
        return {"Location": f"https://r2.example.invalid/{Key}"}

    def abort_multipart_upload(self, *, Bucket, Key, UploadId):
        self.calls.append(("abort_multipart_upload", Bucket, Key, UploadId))


@pytest.fixture
def fake_s3(monkeypatch):
    client = _FakeS3Client()
    monkeypatch.setattr(ml_storage, "_client_and_bucket", lambda: (client, "test-bucket"))
    monkeypatch.setattr(ml_storage, "_normalize", lambda key: key)
    return client


def test_multipart_seam_create_presign_complete_abort(fake_s3):
    key = "media_library/2026/11/abc.mp4"

    upload_id = ml_storage.create_multipart_upload(key, content_type="video/mp4")
    assert upload_id == "mock-upload-id"

    url = ml_storage.presign_part_upload(key, upload_id, 3, expires_in=900)
    assert url == "https://r2.example.invalid/3?sig=x"

    parts = [{"PartNumber": 1, "ETag": "etag-1"}, {"PartNumber": 2, "ETag": "etag-2"}]
    ml_storage.complete_multipart_upload(key, upload_id, parts)

    ml_storage.abort_multipart_upload(key, "other-upload-id")

    assert fake_s3.calls == [
        ("create_multipart_upload", "test-bucket", key, "video/mp4"),
        (
            "generate_presigned_url",
            "upload_part",
            {"Bucket": "test-bucket", "Key": key, "UploadId": "mock-upload-id", "PartNumber": 3},
            900,
        ),
        ("complete_multipart_upload", "test-bucket", key, "mock-upload-id", {"Parts": parts}),
        ("abort_multipart_upload", "test-bucket", key, "other-upload-id"),
    ]


@pytest.mark.django_db
def test_head_object_size_local_branch():
    # Real local branch (no mocks): size off default_storage, None when gone.
    key = ml_storage.generate_storage_key("clip.mp4")
    assert ml_storage.head_object_size(key) is None

    from django.core.files.base import ContentFile

    default_storage.save(key, ContentFile(b"x" * 1234))
    try:
        assert ml_storage.head_object_size(key) == 1234
    finally:
        default_storage.delete(key)


@pytest.mark.django_db
def test_read_object_head_bytes_local_branch():
    key = ml_storage.generate_storage_key("clip.mp4")
    from django.core.files.base import ContentFile

    default_storage.save(key, ContentFile(MP4 + b"\x00" * 100))
    try:
        assert ml_storage.read_object_head_bytes(key, 32) == (MP4 + b"\x00" * 100)[:32]
        assert ml_storage.read_object_head_bytes(key, 8) == MP4[:8]
    finally:
        default_storage.delete(key)

    with pytest.raises(FileNotFoundError):
        ml_storage.read_object_head_bytes(key, 32)


@pytest.mark.django_db
def test_init_chunked_upload_local_mode_and_caps():
    from apps.media_library.services import (
        CHUNKED_UPLOAD_TTL_SECONDS,
        UPLOAD_CHUNK_SIZE,
        init_chunked_upload,
    )
    from apps.media_library.models import PendingUpload

    org = Organization.objects.create(name="Org")
    ws = Workspace.objects.create(organization=org, name="WS")

    pending = init_chunked_upload(
        organization=org,
        workspace=ws,
        created_by=None,
        declared_filename="clip.mp4",
        total_bytes=10 * 1024 * 1024,
        media_type="video",
        content_type="video/mp4",
    )
    assert pending.upload_mode == PendingUpload.UploadMode.SERVER_APPEND
    assert pending.s3_upload_id == ""
    assert pending.chunk_size == UPLOAD_CHUNK_SIZE
    assert pending.total_parts == 2
    assert pending.expires_at > timezone.now() + timedelta(seconds=CHUNKED_UPLOAD_TTL_SECONDS - 60)

    with pytest.raises(ValidationError):
        init_chunked_upload(
            organization=org,
            workspace=ws,
            created_by=None,
            declared_filename="pic.png",
            total_bytes=MAX_FILE_SIZES["image"] + 1,
            media_type="image",
        )
    with pytest.raises(ValidationError):
        init_chunked_upload(
            organization=org,
            workspace=ws,
            created_by=None,
            declared_filename="clip.mp4",
            total_bytes=100,
            media_type="audio",
        )
