"""Composer validation follows the destination platform for Zernio accounts.

A TikTok-via-Zernio post needs a video exactly like a direct TikTok post;
a Zernio Pinterest post is never blocked for a board, because Zernio has
no boards to choose from.
"""

from django.test import TestCase

from apps.composer.services import publish_blocker
from apps.media_library.models import MediaAsset
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace
from apps.zernio.models import ZernioAccountBinding, ZernioApiKey

IMAGE = {MediaAsset.MediaType.IMAGE}
VIDEO = {MediaAsset.MediaType.VIDEO}
SK_KEY = "sk_" + "e" * 64


class ZernioBlockerTest(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Org")
        self.workspace = Workspace.objects.create(organization=self.org, name="WS")

    def _account(self, destination):
        account = SocialAccount.objects.create(
            workspace=self.workspace,
            platform="zernio",
            account_platform_id=f"z-{destination}",
            account_name="BeritaKenapa",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        key = ZernioApiKey.objects.create(workspace=self.workspace, label="Key 1", api_key=SK_KEY)
        ZernioAccountBinding.objects.create(
            social_account=account,
            api_key=key,
            zernio_account_id=f"z-{destination}",
            zernio_profile_id="prof1",
            destination_platform=destination,
        )
        return SocialAccount.objects.get(pk=account.pk)

    def test_zernio_tiktok_needs_a_video_and_says_tiktok(self):
        account = self._account("tiktok")
        for media in (set(), IMAGE):
            reason = publish_blocker(account, media_types=media)
            assert reason.startswith("TikTok can only publish videos"), reason
        assert publish_blocker(account, media_types=VIDEO) == ""

    def test_zernio_youtube_needs_a_video_and_says_youtube(self):
        account = self._account("youtube")
        assert publish_blocker(account, media_types=IMAGE).startswith(
            "YouTube can only publish videos"
        )
        assert publish_blocker(account, media_types=VIDEO) == ""

    def test_zernio_pinterest_is_never_blocked_for_a_board(self):
        # Boards are a direct-Pinterest requirement; Zernio has none, so the
        # board gate must not strand a Zernio-routed pin.
        account = self._account("pinterest")
        assert publish_blocker(account, media_types=IMAGE) == ""

    def test_zernio_instagram_needs_media(self):
        account = self._account("instagram")
        assert "needs an image or a video" in publish_blocker(account, media_types=set())
        assert publish_blocker(account, media_types=IMAGE) == ""
