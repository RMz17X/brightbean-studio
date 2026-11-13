"""Effective (destination) platform for Zernio-routed accounts.

Zernio is a transport, not a platform: an account bound through Zernio
must behave like its destination platform everywhere platform rules
apply, while publish routing still uses the raw ``platform="zernio"``.
"""

import pytest
from django.urls import reverse

from apps.social_accounts.models import SocialAccount
from apps.zernio.models import ZernioAccountBinding, ZernioApiKey

SK_KEY = "sk_" + "d" * 64


@pytest.fixture
def workspace(db, organization):
    from apps.workspaces.models import Workspace

    return Workspace.objects.create(name="Display WS", organization=organization)


def _account(workspace, platform, **kwargs):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform=platform,
        account_platform_id=kwargs.pop("account_platform_id", f"{platform}-1"),
        account_name=kwargs.pop("account_name", f"{platform} acct"),
        connection_status=kwargs.pop("connection_status", "connected"),
        **kwargs,
    )


def _bind(workspace, account, destination):
    key = ZernioApiKey.objects.create(workspace=workspace, label="Key 1", api_key=SK_KEY)
    return ZernioAccountBinding.objects.create(
        social_account=account,
        api_key=key,
        zernio_account_id=f"z-{destination}",
        zernio_profile_id="prof1",
        destination_platform=destination,
    )


@pytest.mark.django_db
class TestDisplayPlatform:
    def test_direct_account_is_itself(self, workspace):
        acc = _account(workspace, "tiktok")
        assert acc.display_platform == "tiktok"
        assert acc.is_via_zernio is False
        assert acc.transport is None
        assert acc.display_platform_label == "TikTok"
        assert acc.char_limit == 2200
        assert str(acc) == "tiktok acct (TikTok)"

    def test_zernio_tiktok_behaves_like_tiktok(self, workspace):
        acc = _account(workspace, "zernio", account_name="BeritaKenapa")
        _bind(workspace, acc, "tiktok")
        acc = SocialAccount.objects.get(pk=acc.pk)
        assert acc.platform == "zernio"  # routing key unchanged
        assert acc.display_platform == "tiktok"
        assert acc.is_via_zernio is True
        assert acc.transport == "zernio"
        assert acc.display_platform_label == "TikTok"
        assert acc.char_limit == 2200
        assert acc.supports_first_comment() is False
        assert acc.platform_icon == "tk"
        assert str(acc) == "BeritaKenapa (TikTok)"

    def test_zernio_youtube_caps_title_and_no_advanced_fields(self, workspace):
        acc = _account(workspace, "zernio", account_name="BeritaKenapa")
        _bind(workspace, acc, "youtube")
        acc = SocialAccount.objects.get(pk=acc.pk)
        assert acc.display_platform == "youtube"
        assert acc.char_limit == 5000
        assert acc.field_config["needs_title"] is True
        assert acc.field_config["title_max_length"] == 100
        # The transport cannot honour the direct providers' advanced extras
        # (thumbnail, made_for_kids, ...), so only the ones Zernio's
        # POST /v1/posts actually carries survive: privacy_status/tags.
        assert acc.field_config["advanced_fields"] == ["privacy_status", "tags"]
        # F1: the transport has no comment endpoint, so a first comment must
        # never be advertised for a Zernio account — even when the
        # destination (YouTube) supports it directly.
        assert acc.field_config["supports_first_comment"] is False
        assert acc.supports_first_comment() is False
        assert acc.platform_icon == "yt"

    def test_direct_youtube_still_supports_first_comment(self, workspace):
        acc = _account(workspace, "youtube")
        assert acc.supports_first_comment() is True

    def test_zernio_facebook_caption_ceiling(self, workspace):
        acc = _account(workspace, "zernio")
        _bind(workspace, acc, "facebook")
        acc = SocialAccount.objects.get(pk=acc.pk)
        assert acc.display_platform == "facebook"
        assert acc.char_limit == 63206

    def test_zernio_without_binding_falls_back(self, workspace):
        acc = _account(workspace, "zernio")
        assert acc.display_platform == "zernio"
        assert acc.transport == "zernio"
        # F7: an unbound Zernio account labels itself "Zernio", not "zernio".
        assert acc.display_platform_label == "Zernio"
        assert acc.char_limit == 2200

    def test_unsaved_zernio_account_does_not_crash(self):
        acc = SocialAccount(platform="zernio")
        assert acc.display_platform == "zernio"
        assert acc.char_limit == 2200


@pytest.fixture
def manager_setup(db, user, organization, workspace):
    from apps.members.models import OrgMembership, WorkspaceMembership

    OrgMembership.objects.create(user=user, organization=organization, org_role="owner")
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role="manager")
    return user


@pytest.mark.django_db
class TestAccountListShowsDestination:
    def test_zernio_account_card_names_destination(self, client, user, manager_setup, workspace):
        acc = _account(workspace, "zernio", account_name="BeritaKenapa")
        _bind(workspace, acc, "tiktok")
        client.force_login(user)
        url = reverse("social_accounts:list", kwargs={"workspace_id": workspace.pk})
        response = client.get(url)
        assert response.status_code == 200
        html = response.content.decode()
        assert "TikTok" in html
        assert "via Zernio" in html


@pytest.mark.django_db
class TestZernioReconnectGuard:
    def test_zernio_reconnect_redirects_to_zernio_page(self, client, user, manager_setup, workspace):
        acc = _account(workspace, "zernio", account_name="BeritaKenapa")
        _bind(workspace, acc, "tiktok")
        client.force_login(user)
        url = reverse("social_accounts:reconnect", kwargs={"workspace_id": workspace.pk, "account_id": acc.pk})
        response = client.post(url)
        assert response.status_code == 302
        assert response.url == reverse("zernio:dashboard", kwargs={"workspace_id": workspace.pk})
