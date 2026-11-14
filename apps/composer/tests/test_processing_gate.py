"""The `processing_status` gate in `publish_blocker` (v1.2.0, Agen B).

A post whose attached media is still being processed must be refused
at schedule time — with a clear message — instead of failing later at
publish time when the platform fetches an unfinished file. Assets
default to `completed`, so existing publishability behaviour is
unchanged unless an asset is actually pending/processing/failed.
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.composer.models import PlatformPost, Post
from apps.composer.services import (
    create_post,
    post_media_processing_statuses,
    publish_blocker,
    require_publishable,
    transition_platform_post,
)
from apps.media_library.models import MediaAsset
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

IMAGE = {MediaAsset.MediaType.IMAGE}
VIDEO = {MediaAsset.MediaType.VIDEO}


class PublishBlockerProcessingTest(TestCase):
    def _account(self, platform):
        return SocialAccount(platform=platform)

    def test_pending_asset_blocks(self):
        reason = publish_blocker(
            self._account("tiktok"),
            media_types=VIDEO,
            processing_statuses=["pending"],
        )
        assert reason == "Media is still processing; wait until processing completes before scheduling."

    def test_processing_and_failed_assets_block(self):
        for status in ("processing", "failed"):
            with self.subTest(status=status):
                reason = publish_blocker(
                    self._account("tiktok"),
                    media_types=VIDEO,
                    processing_statuses=[status],
                )
                assert "still processing" in reason

    def test_completed_asset_passes(self):
        assert publish_blocker(self._account("tiktok"), media_types=VIDEO, processing_statuses=["completed"]) == ""

    def test_no_info_keeps_old_behaviour(self):
        # Callers without asset info (e.g. the CSV import's board-only
        # check) pass no statuses — the gate is skipped, not assumed.
        assert publish_blocker(self._account("tiktok"), media_types=VIDEO) == ""
        assert publish_blocker(self._account("tiktok"), media_types=VIDEO, processing_statuses=[]) == ""

    def test_mixed_assets_block_when_any_is_pending(self):
        reason = publish_blocker(
            self._account("tiktok"),
            media_types=VIDEO,
            processing_statuses=["completed", "pending"],
        )
        assert "still processing" in reason

    def test_media_type_failures_still_win(self):
        # The pre-existing media gate runs first: an image-only TikTok
        # post reports the video problem, not the processing one.
        reason = publish_blocker(
            self._account("tiktok"),
            media_types=IMAGE,
            processing_statuses=["pending"],
        )
        assert reason.startswith("TikTok can only publish videos")


class ServiceSchedulingProcessingTest(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Org")
        self.workspace = Workspace.objects.create(organization=self.org, name="WS")
        self.tiktok = self._account("tiktok")
        self.when = timezone.now() + timedelta(hours=1)

    def _account(self, platform):
        return SocialAccount.objects.create(
            workspace=self.workspace,
            platform=platform,
            account_platform_id=f"{platform}-1",
            account_name=platform,
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )

    def _asset(self, media_type, *, processing_status=None):
        kwargs = {}
        if processing_status is not None:
            kwargs["processing_status"] = processing_status
        return MediaAsset.objects.create(
            organization=self.org,
            workspace=self.workspace,
            file=f"test/{media_type}",
            filename=media_type,
            media_type=media_type,
            mime_type="video/mp4" if media_type == MediaAsset.MediaType.VIDEO else "image/jpeg",
            **kwargs,
        )

    def _create(self, account, *, status="scheduled", media=()):
        return create_post(
            workspace=self.workspace,
            social_account=account,
            caption="hi",
            media_asset_ids=[asset.id for asset in media],
            scheduled_at=self.when if status == "scheduled" else None,
            status=status,
        )

    def test_scheduling_with_a_pending_video_is_refused_and_writes_nothing(self):
        video = self._asset(MediaAsset.MediaType.VIDEO, processing_status=MediaAsset.ProcessingStatus.PENDING)

        with self.assertRaisesMessage(ValueError, "still processing"):
            self._create(self.tiktok, media=[video])

        assert not Post.objects.exists()

    def test_scheduling_with_a_completed_video_is_allowed(self):
        post = self._create(self.tiktok, media=[self._asset(MediaAsset.MediaType.VIDEO)])

        assert post.platform_posts.get().status == PlatformPost.Status.SCHEDULED

    def test_draft_with_pending_video_can_be_scheduled_once_complete(self):
        video = self._asset(MediaAsset.MediaType.VIDEO, processing_status=MediaAsset.ProcessingStatus.PENDING)
        pp = self._create(self.tiktok, status="draft", media=[video]).platform_posts.get()

        with self.assertRaisesMessage(ValueError, "still processing"):
            transition_platform_post(pp, "scheduled", scheduled_at=self.when)
        pp.refresh_from_db()
        assert pp.status == PlatformPost.Status.DRAFT

        MediaAsset.objects.filter(pk=video.pk).update(
            processing_status=MediaAsset.ProcessingStatus.COMPLETED
        )
        transition_platform_post(pp, "scheduled", scheduled_at=self.when)

        pp.refresh_from_db()
        assert pp.status == PlatformPost.Status.SCHEDULED

    def test_require_publishable_reads_statuses_off_the_post(self):
        video = self._asset(MediaAsset.MediaType.VIDEO, processing_status=MediaAsset.ProcessingStatus.PENDING)
        pp = self._create(self.tiktok, status="draft", media=[video]).platform_posts.get()

        assert post_media_processing_statuses(pp.post) == {"pending"}
        with self.assertRaisesMessage(ValueError, "still processing"):
            require_publishable(pp)

    def test_asset_without_a_stored_file_does_not_gate(self):
        # Consistent with usable_media_types: an asset with no file can't
        # satisfy a platform, so its processing state is irrelevant here
        # (the media gate itself already refuses the schedule).
        video = self._asset(MediaAsset.MediaType.VIDEO, processing_status=MediaAsset.ProcessingStatus.PENDING)
        MediaAsset.objects.filter(pk=video.pk).update(file="")

        with self.assertRaisesMessage(ValueError, "TikTok can only publish videos"):
            self._create(self.tiktok, media=[video])
