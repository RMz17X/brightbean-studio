"""Tests for the account-card profile form (``update_profile`` view).

The provider is never touched: the service layer is stubbed at the
``views.profile_service.update_account_profile`` seam. Rendering
tests assert the card offers a form only where the platform can
actually update, and a note everywhere else.
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from apps.members.models import OrgMembership, WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.social_accounts.profile_service import MANUAL_BIO_MESSAGE, ProfileServiceError
from apps.zernio.models import ZernioAccountBinding, ZernioApiKey

SK_KEY = "sk_" + "d" * 64


@pytest.fixture
def workspace(db, organization):
    from apps.workspaces.models import Workspace

    return Workspace.objects.create(name="Profile View WS", organization=organization)


@pytest.fixture
def manager_setup(db, user, organization, workspace):
    OrgMembership.objects.create(user=user, organization=organization, org_role="owner")
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role="manager")
    return user


@pytest.fixture
def authenticated_client(client, user, manager_setup):
    client.force_login(user)
    return client


def _account(workspace, platform, **kwargs):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform=platform,
        account_platform_id=kwargs.pop("account_platform_id", f"{platform}-1"),
        account_name=kwargs.pop("account_name", f"{platform} acct"),
        connection_status=kwargs.pop(
            "connection_status", SocialAccount.ConnectionStatus.CONNECTED
        ),
        **kwargs,
    )


def _bind_zernio(workspace, account, destination):
    key = ZernioApiKey.objects.create(workspace=workspace, label="Key 1", api_key=SK_KEY)
    return ZernioAccountBinding.objects.create(
        social_account=account,
        api_key=key,
        zernio_account_id=f"z-{destination}",
        zernio_profile_id="prof1",
        destination_platform=destination,
    )


def _list_url(workspace):
    return reverse("social_accounts:list", kwargs={"workspace_id": workspace.id})


def _update_url(workspace, account):
    return reverse(
        "social_accounts:update_profile",
        kwargs={"workspace_id": workspace.id, "account_id": account.id},
    )


@pytest.mark.django_db
class TestProfileFormRendering:
    def test_youtube_direct_shows_description_form(self, authenticated_client, workspace):
        _account(workspace, "youtube")
        r = authenticated_client.get(_list_url(workspace))
        assert r.status_code == 200
        assert b"Channel Description" in r.content
        assert b"Save description" in r.content

    def test_facebook_direct_shows_page_info_form(self, authenticated_client, workspace):
        _account(workspace, "facebook")
        r = authenticated_client.get(_list_url(workspace))
        assert r.status_code == 200
        assert b"Page Info" in r.content
        assert b"Save Page info" in r.content

    def test_tiktok_shows_manual_note_not_a_form(self, authenticated_client, workspace):
        _account(workspace, "tiktok")
        r = authenticated_client.get(_list_url(workspace))
        assert r.status_code == 200
        assert MANUAL_BIO_MESSAGE.encode() in r.content
        assert b"Save description" not in r.content
        assert b"Save Page info" not in r.content

    def test_youtube_via_zernio_shows_zernio_note(self, authenticated_client, workspace):
        acc = _account(workspace, "zernio", account_name="YT via Zernio")
        _bind_zernio(workspace, acc, "youtube")
        r = authenticated_client.get(_list_url(workspace))
        assert r.status_code == 200
        assert "Update deskripsi tidak tersedia via Zernio".encode() in r.content
        assert b"Save description" not in r.content


@pytest.mark.django_db
class TestUpdateProfileView:
    def test_post_calls_service_and_redirects(
        self, authenticated_client, workspace, monkeypatch
    ):
        acc = _account(workspace, "youtube")
        seen = {}

        def fake_update(account, **kwargs):
            seen["account_id"] = account.id
            seen.update(kwargs)
            return {"platform": "youtube", "updated_fields": ["description"]}

        monkeypatch.setattr(
            "apps.social_accounts.views.profile_service.update_account_profile",
            fake_update,
        )
        r = authenticated_client.post(
            _update_url(workspace, acc), {"description": "New bio"}
        )
        assert r.status_code == 302
        assert r.url == _list_url(workspace)
        assert seen["account_id"] == acc.id
        assert seen["description"] == "New bio"
        assert seen["about"] is None

    def test_post_surfaces_service_refusal_as_redirect(
        self, authenticated_client, workspace, monkeypatch
    ):
        acc = _account(workspace, "tiktok")

        def fake_update(account, **kwargs):
            raise ProfileServiceError(MANUAL_BIO_MESSAGE, status_code=422)

        monkeypatch.setattr(
            "apps.social_accounts.views.profile_service.update_account_profile",
            fake_update,
        )
        r = authenticated_client.post(
            _update_url(workspace, acc), {"description": "Nope"}
        )
        assert r.status_code == 302
        assert r.url == _list_url(workspace)

    def test_post_requires_login(self, client, workspace):
        acc = _account(workspace, "youtube")
        r = client.post(_update_url(workspace, acc), {"description": "Nope"})
        assert r.status_code == 302
        assert "/login" in r.url or "login" in r.url
