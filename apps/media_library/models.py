"""Media Library models (F-6.1) - media asset storage and management."""

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from .managers import MediaAssetManager


class MediaFolder(models.Model):
    """Folder for organizing media assets. Max 3 levels of nesting."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="media_folders",
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="media_folders",
        null=True,
        blank=True,
    )
    parent_folder = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="subfolders",
    )
    name = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "media_library_folder"
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "parent_folder", "name"],
                name="unique_folder_name_per_parent",
            ),
        ]
        ordering = ["name"]

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        if self.parent_folder:
            depth = 1
            current = self.parent_folder
            while current.parent_folder:
                depth += 1
                current = current.parent_folder
                if depth >= 3:
                    raise ValidationError("Folders cannot be nested more than 3 levels deep.")

    @property
    def depth(self):
        d = 0
        current = self.parent_folder
        while current:
            d += 1
            current = current.parent_folder
        return d


class MediaAsset(models.Model):
    """A media file (image, video, GIF) uploaded to a workspace's media library.

    Stores the original file plus processed variants for different platforms.
    When workspace is null, the asset belongs to the shared org-wide library.
    """

    class MediaType(models.TextChoices):
        IMAGE = "image", "Image"
        VIDEO = "video", "Video"
        GIF = "gif", "GIF"
        DOCUMENT = "document", "Document"

    class ProcessingStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="media_assets",
        null=True,
        blank=True,
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="media_assets",
        null=True,
        blank=True,
    )
    folder = models.ForeignKey(
        MediaFolder,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assets",
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_media",
    )

    # File info
    file = models.FileField(upload_to="media_library/%Y/%m/")
    filename = models.CharField(max_length=255)
    media_type = models.CharField(max_length=20, choices=MediaType.choices)
    mime_type = models.CharField(max_length=100, blank=True, default="")
    file_size = models.PositiveBigIntegerField(default=0, help_text="File size in bytes.")

    # Image/video dimensions
    width = models.PositiveIntegerField(default=0)
    height = models.PositiveIntegerField(default=0)
    duration = models.FloatField(default=0, help_text="Video duration in seconds.")

    # Thumbnail for videos and large images
    thumbnail = models.ImageField(upload_to="media_library/thumbs/%Y/%m/", blank=True)

    # Metadata
    alt_text = models.TextField(blank=True, default="")
    title = models.CharField(max_length=255, blank=True, default="")
    tags = models.JSONField(default=list, blank=True)
    is_starred = models.BooleanField(default=False)

    # Attribution for stock media
    source = models.CharField(max_length=50, blank=True, default="", help_text="e.g., 'upload', 'unsplash', 'pexels'")
    source_url = models.URLField(blank=True, default="")
    attribution = models.TextField(blank=True, default="")

    # Processing
    processing_status = models.CharField(
        max_length=20,
        choices=ProcessingStatus.choices,
        default=ProcessingStatus.COMPLETED,
    )
    processed_variants = models.JSONField(
        default=dict,
        blank=True,
        help_text="Dict of platform-specific processed versions: {'instagram': {'file': 'path', 'width': 1080}}",
    )

    # Version tracking
    current_version = models.ForeignKey(
        "MediaAssetVersion",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = MediaAssetManager()

    class Meta:
        db_table = "media_library_media_asset"
        indexes = [
            models.Index(fields=["organization", "workspace", "media_type", "created_at"]),
            models.Index(fields=["organization", "workspace", "is_starred"]),
            models.Index(fields=["folder"]),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return self.filename

    @property
    def is_image(self):
        return self.media_type == self.MediaType.IMAGE

    @property
    def is_video(self):
        return self.media_type == self.MediaType.VIDEO

    @property
    def is_shared(self):
        return self.workspace_id is None

    @property
    def aspect_ratio(self):
        if self.width and self.height:
            return round(self.width / self.height, 2)
        return None

    @property
    def file_extension(self):
        if "." in self.filename:
            return self.filename.rsplit(".", 1)[-1].lower()
        return ""

    @property
    def file_size_display(self):
        """Human-readable file size."""
        size = self.file_size
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"

    # Aliases for template compatibility
    @property
    def original_filename(self):
        return self.filename

    @property
    def file_type(self):
        return self.media_type

    @property
    def human_file_size(self):
        return self.file_size_display

    @property
    def file_size_bytes(self):
        return self.file_size

    @property
    def duration_seconds(self):
        return self.duration if self.duration else None


class MediaAssetVersion(models.Model):
    """Version history for edited media assets. Each edit creates a new version."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    media_asset = models.ForeignKey(
        MediaAsset,
        on_delete=models.CASCADE,
        related_name="versions",
    )
    version_number = models.PositiveIntegerField()
    file = models.FileField(upload_to="media_library/versions/%Y/%m/")
    thumbnail = models.ImageField(upload_to="media_library/thumbs/%Y/%m/", blank=True, default="")
    change_description = models.CharField(max_length=500, blank=True, default="")
    file_size = models.PositiveBigIntegerField(default=0)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    duration = models.FloatField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="media_versions",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "media_library_asset_version"
        unique_together = [("media_asset", "version_number")]
        ordering = ["-version_number"]

    def __str__(self):
        return f"{self.media_asset.filename} v{self.version_number}"

    @property
    def file_size_bytes(self):
        return self.file_size

    @property
    def duration_seconds(self):
        return self.duration


class PendingUpload(models.Model):
    """A presigned direct-to-storage upload awaiting server-side finalization.

    Created by the MCP ``request_media_upload`` tool (which presigns a POST for
    ``storage_key``) and consumed by ``finalize_media_upload``. The row binds the
    server-chosen key to one workspace + user so a finalize call can't register
    an arbitrary or cross-tenant object, and it carries the idempotency state
    (``finalized_at`` + ``media_asset``) that a rowless OAuth caller can't get
    from the REST ``IdempotencyRecord`` path.

    The same row also backs chunked/resumable uploads (``POST
    /api/v1/media/uploads/``): a chunked session sets ``upload_mode`` to
    ``"s3_multipart"`` (S3/R2 backend) or ``"server_append"`` (local backend)
    and records its parts in ``MediaUploadPart``. Legacy presigned-POST rows
    keep ``upload_mode`` empty — they are not chunked sessions.
    """

    class UploadMode(models.TextChoices):
        S3_MULTIPART = "s3_multipart", "S3 Multipart"
        SERVER_APPEND = "server_append", "Server Append"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="pending_uploads",
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="pending_uploads",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pending_uploads",
    )
    # The exact key we presigned. Unique so a key is issued — and finalized — once.
    storage_key = models.CharField(max_length=1024, unique=True)
    declared_content_type = models.CharField(max_length=100, blank=True, default="")
    declared_filename = models.CharField(max_length=255)
    # Edge size cap baked into the presigned POST; NOT trusted for the asset —
    # finalize re-derives the real size from the stored object.
    max_bytes = models.PositiveBigIntegerField()
    expires_at = models.DateTimeField()
    finalized_at = models.DateTimeField(null=True, blank=True)
    media_asset = models.ForeignKey(
        MediaAsset,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    # Chunked-upload session state (v1.2.0). Empty/0 on legacy presigned-POST
    # rows, which are not chunked sessions.
    upload_mode = models.CharField(max_length=20, choices=UploadMode.choices, blank=True, default="")
    total_bytes = models.PositiveBigIntegerField(default=0)
    chunk_size = models.PositiveBigIntegerField(default=0)
    s3_upload_id = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "media_library_pending_upload"
        indexes = [
            models.Index(fields=["expires_at", "finalized_at"]),
        ]

    def __str__(self):
        return f"PendingUpload {self.id} -> {self.storage_key}"

    @property
    def is_chunked(self) -> bool:
        """True for a chunked-upload session (as opposed to a legacy presigned-POST row)."""
        return self.upload_mode in (self.UploadMode.S3_MULTIPART, self.UploadMode.SERVER_APPEND)

    @property
    def total_parts(self) -> int:
        """Number of parts the declared total splits into (0 for non-chunked rows)."""
        if not self.is_chunked or not self.chunk_size or not self.total_bytes:
            return 0
        return -(-int(self.total_bytes) // int(self.chunk_size))

    def received_part_numbers(self) -> list[int]:
        """Part numbers already recorded for this session, ascending."""
        return list(self.parts.order_by("part_number").values_list("part_number", flat=True))

    def uploaded_bytes(self) -> int:
        """Bytes recorded across all received parts."""
        from django.db.models import Sum

        return int(self.parts.aggregate(total=Sum("size"))["total"] or 0)

    def next_part(self) -> int | None:
        """First missing part number, or None when every part is present."""
        received = set(self.received_part_numbers())
        for number in range(1, self.total_parts + 1):
            if number not in received:
                return number
        return None

    def missing_part_numbers(self) -> list[int]:
        """Part numbers 1..total_parts with no recorded row, ascending."""
        received = set(self.received_part_numbers())
        return [n for n in range(1, self.total_parts + 1) if n not in received]


class MediaUploadPart(models.Model):
    """One received chunk of a chunked upload session.

    The source of truth for resume (which parts arrived) and for S3's
    ``CompleteMultipartUpload`` (the ordered ``(PartNumber, ETag)`` list).
    ``etag`` is S3's ETag for ``s3_multipart`` sessions and the hex sha256 of
    the part bytes for ``server_append`` sessions.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    pending_upload = models.ForeignKey(
        PendingUpload,
        on_delete=models.CASCADE,
        related_name="parts",
    )
    part_number = models.PositiveIntegerField()
    etag = models.CharField(max_length=255)
    size = models.PositiveBigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "media_library_upload_part"
        constraints = [
            models.UniqueConstraint(
                fields=["pending_upload", "part_number"],
                name="unique_part_number_per_upload",
            ),
        ]
        ordering = ["part_number"]

    def __str__(self):
        return f"MediaUploadPart {self.pending_upload_id} #{self.part_number}"
