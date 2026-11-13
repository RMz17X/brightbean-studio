"""Composer panel parity for Zernio-routed (transport) accounts.

A via-Zernio account gets the same composer panel as the same platform
direct — gated on the destination platform — except for the options the
transport cannot carry (YouTube thumbnail / made_for_kids, TikTok brand
disclosure / cover frame, Facebook Reel hint, Pinterest board / link /
alt text / cover), which the panel hides and the save refuses to store.
The payload mapping itself (privacy -> visibility, TikTok settings) is
pinned in tests/providers/test_zernio.py.
"""

import json
import re

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.composer.models import PlatformPost, Post
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace
from apps.zernio.models import ZernioAccountBinding, ZernioApiKey

SK_KEY = "sk_" + "e" * 64


def _make_workspace():
    user = User.objects.create_user(
        email="owner@example.com",
        password="testpass123",
        tos_accepted_at=timezone.now(),
    )
    org = Organization.objects.create(name="Test Org")
    workspace = Workspace.objects.create(organization=org, name="Test Workspace")
    OrgMembership.objects.create(user=user, organization=org, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    return user, org, workspace


class ZernioComposerParityTests(TestCase):
    def setUp(self):
        self.user, self.org, self.workspace = _make_workspace()
        self.client.force_login(self.user)
        self.save_url = reverse("composer:save_post", kwargs={"workspace_id": self.workspace.id})
        self.compose_url = reverse("composer:compose", kwargs={"workspace_id": self.workspace.id})

    def _account(self, platform, name="BeritaKenapa", account_platform_id=None):
        return SocialAccount.objects.create(
            workspace=self.workspace,
            platform=platform,
            account_platform_id=account_platform_id or f"{platform}-1",
            account_name=name,
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )

    def _zernio_account(self, destination):
        account = self._account("zernio", account_platform_id=f"zernio-{destination}")
        key = ZernioApiKey.objects.create(workspace=self.workspace, label="Key 1", api_key=SK_KEY)
        ZernioAccountBinding.objects.create(
            social_account=account,
            api_key=key,
            zernio_account_id=f"z-{destination}",
            zernio_profile_id="prof1",
            destination_platform=destination,
        )
        return SocialAccount.objects.get(pk=account.pk)

    def _save(self, account, **panel_fields):
        payload = {
            "action": "save_draft",
            "title": "Test post",
            "caption": "body",
            "selected_accounts": str(account.id),
        }
        payload.update(panel_fields)
        response = self.client.post(self.save_url, data=payload)
        self.assertIn(response.status_code, (200, 204, 302), response.content[:500])
        post = Post.objects.filter(workspace=self.workspace).order_by("-created_at").first()
        self.assertIsNotNone(post)
        return PlatformPost.objects.get(post=post, social_account=account)

    # -- field_config: only Zernio-carried advanced fields survive --------

    def test_zernio_youtube_keeps_only_privacy_and_tags(self):
        account = self._zernio_account("youtube")
        assert account.field_config["advanced_fields"] == ["privacy_status", "tags"]
        assert account.field_config["needs_title"] is True
        assert account.field_config["supports_first_comment"] is False

    def test_zernio_pinterest_has_no_advanced_fields(self):
        account = self._zernio_account("pinterest")
        assert account.field_config["advanced_fields"] == []
        assert account.field_config["needs_title"] is True

    def test_zernio_tiktok_never_advertises_first_comment(self):
        account = self._zernio_account("tiktok")
        assert account.field_config["supports_first_comment"] is False
        assert account.supports_first_comment() is False

    def test_direct_youtube_advanced_fields_unchanged(self):
        account = self._account("youtube")
        assert account.field_config["advanced_fields"] == [
            "made_for_kids",
            "privacy_status",
            "tags",
            "thumbnail",
        ]

    # -- save: YouTube -----------------------------------------------------

    def test_zernio_youtube_save_stores_only_supported_options(self):
        account = self._zernio_account("youtube")
        pp = self._save(
            account,
            **{
                f"yt_privacy_status_{account.id}": "unlisted",
                f"yt_tags_{account.id}": "berita, rupiah",
                f"yt_made_for_kids_{account.id}": "true",
                f"yt_thumbnail_asset_id_{account.id}": "00000000-0000-0000-0000-000000000000",
            },
        )
        assert pp.platform_extra == {"privacy_status": "unlisted", "tags": ["berita", "rupiah"]}

    def test_direct_youtube_save_stores_all_options(self):
        account = self._account("youtube")
        pp = self._save(
            account,
            **{
                f"yt_privacy_status_{account.id}": "unlisted",
                f"yt_tags_{account.id}": "berita, rupiah",
                f"yt_made_for_kids_{account.id}": "true",
                f"yt_thumbnail_asset_id_{account.id}": "00000000-0000-0000-0000-000000000000",
            },
        )
        assert pp.platform_extra["privacy_status"] == "unlisted"
        assert pp.platform_extra["tags"] == ["berita", "rupiah"]
        assert pp.platform_extra["self_declared_made_for_kids"] is True
        assert pp.platform_extra["thumbnail_asset_id"] == "00000000-0000-0000-0000-000000000000"

    # -- save: TikTok ------------------------------------------------------

    def _tiktok_panel(self, account):
        return {
            f"tiktok_privacy_level_{account.id}": "SELF_ONLY",
            f"tiktok_allow_comment_{account.id}": "true",
            # duet deliberately absent -> disable_duet True
            f"tiktok_allow_stitch_{account.id}": "true",
            f"tiktok_is_aigc_{account.id}": "true",
            f"tiktok_brand_organic_{account.id}": "true",
            f"tiktok_brand_content_{account.id}": "true",
            f"tiktok_video_cover_timestamp_ms_{account.id}": "1500",
        }

    def test_zernio_tiktok_save_drops_brand_and_cover(self):
        account = self._zernio_account("tiktok")
        pp = self._save(account, **self._tiktok_panel(account))
        assert pp.platform_extra == {
            "privacy_level": "SELF_ONLY",
            "disable_comment": False,
            "disable_duet": True,
            "disable_stitch": False,
            "is_aigc": True,
        }

    def test_direct_tiktok_save_keeps_brand_and_cover(self):
        account = self._account("tiktok")
        pp = self._save(account, **self._tiktok_panel(account))
        assert pp.platform_extra["brand_organic_toggle"] is True
        assert pp.platform_extra["brand_content_toggle"] is True
        assert pp.platform_extra["video_cover_timestamp_ms"] == 1500
        assert pp.platform_extra["privacy_level"] == "SELF_ONLY"

    # -- save: Facebook / Pinterest (no Zernio padanan) --------------------

    def test_zernio_facebook_save_drops_stale_reel_hint(self):
        account = self._zernio_account("facebook")
        post = Post.objects.create(workspace=self.workspace, author=self.user, title="T", caption="c")
        PlatformPost.objects.create(post=post, social_account=account, platform_extra={"post_type": "reel"})
        url = reverse(
            "composer:save_post_edit",
            kwargs={"workspace_id": self.workspace.id, "post_id": post.id},
        )
        response = self.client.post(
            url,
            data={
                "action": "save_draft",
                "title": "T",
                "caption": "c",
                "selected_accounts": str(account.id),
                f"facebook_panel_{account.id}": "1",
                f"facebook_post_type_{account.id}": "reel",
            },
        )
        self.assertIn(response.status_code, (200, 204, 302), response.content[:500])
        pp = PlatformPost.objects.get(post=post, social_account=account)
        assert "post_type" not in (pp.platform_extra or {})

    def test_zernio_pinterest_save_stores_no_board(self):
        account = self._zernio_account("pinterest")
        pp = self._save(account, **{f"pin_board_id_{account.id}": "board123"})
        assert "board_id" not in (pp.platform_extra or {})

    # -- compose page: char_limits follow the destination ------------------

    def test_compose_char_limits_follow_destination(self):
        yt_z = self._zernio_account("youtube")
        tt_z = self._zernio_account("tiktok")
        yt_direct = self._account("youtube", name="Direct YT")
        response = self.client.get(self.compose_url)
        assert response.status_code == 200
        body = response.content.decode("utf-8")
        match = re.search(
            r'<script id="composer-char-limits" type="application/json">(.*?)</script>',
            body,
            re.DOTALL,
        )
        assert match, "composer-char-limits script tag missing"
        limits = json.loads(match.group(1))
        assert limits[str(yt_z.id)]["platform"] == "youtube"
        assert limits[str(yt_z.id)]["transport"] == "zernio"
        assert limits[str(yt_z.id)]["advanced_fields"] == ["privacy_status", "tags"]
        assert limits[str(yt_z.id)]["limit"] == 5000
        assert limits[str(yt_z.id)]["youtube_url"].startswith("https://www.youtube.com/channel/")
        assert limits[str(tt_z.id)]["platform"] == "tiktok"
        assert limits[str(tt_z.id)]["transport"] == "zernio"
        assert limits[str(yt_direct.id)]["platform"] == "youtube"
        assert limits[str(yt_direct.id)]["transport"] is None
        assert limits[str(yt_direct.id)]["advanced_fields"] == [
            "made_for_kids",
            "privacy_status",
            "tags",
            "thumbnail",
        ]

    # -- TikTok creator-info endpoint --------------------------------------

    def test_tiktok_creator_info_via_zernio_falls_back(self):
        account = self._zernio_account("tiktok")
        url = reverse(
            "composer:tiktok_creator_info",
            kwargs={"workspace_id": self.workspace.id, "account_id": account.id},
        )
        response = self.client.get(url)
        assert response.status_code == 200
        data = response.json()
        assert data["available"] is False
        assert data["privacy_level_options"] == []

    def test_tiktok_creator_info_non_tiktok_404s(self):
        account = self._zernio_account("youtube")
        url = reverse(
            "composer:tiktok_creator_info",
            kwargs={"workspace_id": self.workspace.id, "account_id": account.id},
        )
        response = self.client.get(url)
        assert response.status_code == 404

    def test_web_save_keeps_api_set_youtube_category_id(self):
        # API-created state: category_id lives in platform_extra (see
        # apps/composer/services.py — youtube_category_id). A later web
        # composer save must not wipe it: the YouTube branch merges onto
        # the stored extras instead of overwriting them wholesale.
        account = self._account("youtube")
        post = Post.objects.create(workspace=self.workspace, author=self.user, title="T", caption="c")
        PlatformPost.objects.create(
            post=post,
            social_account=account,
            platform_extra={
                "privacy_status": "public",
                "tags": [],
                "category_id": "10",
            },
        )
        url = reverse(
            "composer:save_post_edit",
            kwargs={"workspace_id": self.workspace.id, "post_id": post.id},
        )
        response = self.client.post(
            url,
            data={
                "action": "save_draft",
                "title": "T",
                "caption": "c",
                "selected_accounts": str(account.id),
                f"yt_privacy_status_{account.id}": "private",
                f"yt_tags_{account.id}": "berita",
            },
        )
        self.assertIn(response.status_code, (200, 204, 302), response.content[:500])
        pp = PlatformPost.objects.get(post=post, social_account=account)
        assert pp.platform_extra.get("category_id") == "10"
        assert pp.platform_extra["privacy_status"] == "private"
        assert pp.platform_extra["tags"] == ["berita"]
