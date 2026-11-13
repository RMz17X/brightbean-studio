"""Tests for ``PATCH /api/v1/accounts/{id}/profile``.

No platform call is ever made: the provider is faked at the
``profile_service._get_provider_for_platform`` seam, and the fake
implements the two provider methods the service calls
(``update_channel_description`` / ``update_page_info``) with canned
returns. The YouTube method does not exist in the provider yet at
the time this test was written (parallel worker) — the fake stands
in for the assumed signature
``update_channel_description(access_token, *, description, channel_id=None)``.
"""

from __future__ import annotations

import json

import pytest
from django.test import Client
from django.utils import timezone

from apps.api_keys import services
from apps.members.models import PERMISSION_KEYS, OrgMembership, WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.zernio.models import ZernioAccountBinding, ZernioApiKey
from providers.exceptions import APIError

SK_KEY = "sk_" + "d" * 64


class FakeProfileProvider:
    """Stand-in for YouTubeProvider/FacebookProvider profile methods."""

    def __init__(self):
        self.calls = []
        self.fail_scope = False
        self.include_analytics_scopes = False

    def update_channel_description(self, token, *, description, channel_id=None):
        self.calls.append(("update_channel_description", token, description, channel_id))
        if self.fail_scope:
            raise APIError(
                "insufficientPermissions: the request is forbidden",
                status_code=403,
                platform="youtube",
            )
        return {"channel_id": channel_id, "description": description}

    def update_page_info(self, token, page_id, *, about=None, description=None, website=None, phone=None):
        self.calls.append(("update_page_info", token, page_id, about, description, website, phone))
        fields = {
            name: value
            for name, value in (
                ("about", about),
                ("description", description),
                ("website", website),
                ("phone", phone),
            )
            if value is not None
        }
        return {"page_id": page_id, "updated_fields": sorted(fields), "success": True}


@pytest.fixture
def provider(monkeypatch):
    fake = FakeProfileProvider()
    monkeypatch.setattr(
        "apps.social_accounts.profile_service._get_provider_for_platform",
        lambda platform, org_id: fake,
    )
    return fake


@pytest.fixture
def user(db):
    from apps.accounts.models import User

    return User.objects.create_user(
        email="profile-api-owner@example.com",
        password="secret-123",
        name="Profile API Owner",
        tos_accepted_at=timezone.now(),
    )


@pytest.fixture
def organization(db):
    from apps.organizations.models import Organization

    return Organization.objects.create(name="Profile API Org")


@pytest.fixture
def workspace(db, organization):
    from apps.workspaces.models import Workspace

    return Workspace.objects.create(name="Profile API WS", organization=organization)


@pytest.fixture
def owner_memberships(db, user, organization, workspace):
    OrgMembership.objects.create(user=user, organization=organization, org_role=OrgMembership.OrgRole.OWNER)
    return WorkspaceMembership.objects.create(
        user=user,
        workspace=workspace,
        workspace_role=WorkspaceMembership.WorkspaceRole.OWNER,
    )


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


def _bind(workspace, account, destination):
    key = ZernioApiKey.objects.create(workspace=workspace, label="Key 1", api_key=SK_KEY)
    return ZernioAccountBinding.objects.create(
        social_account=account,
        api_key=key,
        zernio_account_id=f"z-{destination}",
        zernio_profile_id="prof1",
        destination_platform=destination,
    )


@pytest.fixture
def accounts(db, workspace):
    """One account per platform case, all in the key's workspace."""
    yt = _account(workspace, "youtube", account_platform_id="yt-1", account_name="YT Channel")
    yt_zernio = _account(workspace, "zernio", account_platform_id="z-yt-1", account_name="YT via Zernio")
    _bind(workspace, yt_zernio, "youtube")
    tiktok = _account(workspace, "tiktok", account_platform_id="tt-1", account_name="TikTok")
    ig_zernio = _account(workspace, "zernio", account_platform_id="z-ig-1", account_name="IG via Zernio")
    _bind(workspace, ig_zernio, "instagram")
    fb = _account(workspace, "facebook", account_platform_id="fb-page-1", account_name="FB Page")
    fb_zernio = _account(workspace, "zernio", account_platform_id="z-fb-1", account_name="FB via Zernio")
    _bind(workspace, fb_zernio, "facebook")
    return {
        "youtube": yt,
        "youtube_zernio": yt_zernio,
        "tiktok": tiktok,
        "instagram_zernio": ig_zernio,
        "facebook": fb,
        "facebook_zernio": fb_zernio,
    }


@pytest.fixture
def foreign_account(db, organization):
    """Account in a *different* workspace inside the same org."""
    from apps.workspaces.models import Workspace

    other = Workspace.objects.create(name="Other Profile WS", organization=organization)
    return SocialAccount.objects.create(
        workspace=other,
        platform="youtube",
        account_platform_id="yt-other",
        account_name="Other YT",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )


@pytest.fixture
def issued_key(db, user, owner_memberships, workspace, accounts):
    return services.issue_api_key(
        workspace=workspace,
        social_accounts=list(accounts.values()),
        issued_by=user,
        name="profile-smoke",
        permissions=list(PERMISSION_KEYS),
    )


@pytest.fixture
def readonly_key(db, user, owner_memberships, workspace, accounts):
    """Key without create_posts: reads work, writes 403."""
    perms = [p for p in PERMISSION_KEYS if p != "create_posts"]
    return services.issue_api_key(
        workspace=workspace,
        social_accounts=list(accounts.values()),
        issued_by=user,
        name="profile-readonly",
        permissions=perms,
    )


class _SecureClient(Client):
    def generic(self, method, path, *args, **kwargs):
        kwargs["secure"] = True
        return super().generic(method, path, *args, **kwargs)


@pytest.fixture
def api_client(issued_key):
    return _SecureClient(HTTP_AUTHORIZATION=f"Bearer {issued_key.plaintext_token}")


@pytest.fixture
def readonly_client(readonly_key):
    return _SecureClient(HTTP_AUTHORIZATION=f"Bearer {readonly_key.plaintext_token}")


def _patch(client, account_id, payload):
    return client.patch(
        f"/api/v1/accounts/{account_id}/profile",
        data=json.dumps(payload),
        content_type="application/json",
    )


# ---------------------------------------------------------------------------
# Auth / allowlist / permission
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAuth:
    def test_missing_token_returns_401(self, accounts):
        assert _SecureClient().patch(
            f"/api/v1/accounts/{accounts['youtube'].id}/profile",
            data=json.dumps({"description": "x"}),
            content_type="application/json",
        ).status_code == 401

    def test_bad_token_returns_401(self, accounts):
        c = _SecureClient(HTTP_AUTHORIZATION="Bearer <redacted>")
        assert _patch(c, accounts["youtube"].id, {"description": "x"}).status_code == 401

    def test_foreign_account_returns_403(self, api_client, foreign_account, provider):
        r = _patch(api_client, foreign_account.id, {"description": "Nope"})
        assert r.status_code == 403
        assert provider.calls == []

    def test_write_requires_create_posts_permission(self, readonly_client, accounts):
        r = _patch(readonly_client, accounts["youtube"].id, {"description": "Nope"})
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# YouTube direct
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestYouTube:
    def test_update_description_success(self, api_client, accounts, provider):
        yt = accounts["youtube"]
        r = _patch(api_client, yt.id, {"description": "New channel bio"})
        assert r.status_code == 200, r.content
        body = r.json()
        assert body["id"] == str(yt.id)
        assert body["platform"] == "youtube"
        assert body["transport"] is None
        assert body["description"] == "New channel bio"
        assert body["updated_fields"] == ["description"]
        assert ("update_channel_description", "", "New channel bio", "yt-1") in provider.calls

    def test_via_zernio_youtube_is_refused(self, api_client, accounts, provider):
        r = _patch(api_client, accounts["youtube_zernio"].id, {"description": "Nope"})
        assert r.status_code == 422
        assert "via Zernio" in r.json()["detail"]
        assert "langsung" in r.json()["detail"]
        assert provider.calls == []

    def test_description_over_1000_chars_is_rejected(self, api_client, accounts, provider):
        r = _patch(api_client, accounts["youtube"].id, {"description": "x" * 1001})
        assert r.status_code == 422
        assert provider.calls == []

    def test_description_at_1000_chars_is_accepted(self, api_client, accounts, provider):
        r = _patch(api_client, accounts["youtube"].id, {"description": "x" * 1000})
        assert r.status_code == 200, r.content

    def test_insufficient_scope_returns_403_with_reconnect_hint(
        self, api_client, accounts, provider
    ):
        provider.fail_scope = True
        r = _patch(api_client, accounts["youtube"].id, {"description": "Nope"})
        assert r.status_code == 403
        detail = r.json()["detail"]
        assert "Reconnect for Live" in detail
        assert "insufficientPermissions" not in detail

    def test_empty_body_is_rejected(self, api_client, accounts, provider):
        assert _patch(api_client, accounts["youtube"].id, {}).status_code == 422
        assert provider.calls == []


# ---------------------------------------------------------------------------
# Facebook Page direct
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestFacebook:
    def test_update_page_info_success(self, api_client, accounts, provider):
        fb = accounts["facebook"]
        r = _patch(
            api_client,
            fb.id,
            {"about": "About us", "website": "https://example.com", "phone": "+62 21 555"},
        )
        assert r.status_code == 200, r.content
        body = r.json()
        assert body["platform"] == "facebook"
        assert body["about"] == "About us"
        assert body["website"] == "https://example.com"
        assert body["phone"] == "+62 21 555"
        assert body["updated_fields"] == ["about", "phone", "website"]
        assert provider.calls[0][0] == "update_page_info"
        assert provider.calls[0][2] == "fb-page-1"

    def test_website_must_be_a_url(self, api_client, accounts, provider):
        r = _patch(api_client, accounts["facebook"].id, {"website": "not-a-url"})
        assert r.status_code == 422
        assert provider.calls == []

    def test_website_must_be_http(self, api_client, accounts, provider):
        r = _patch(api_client, accounts["facebook"].id, {"website": "ftp://example.com"})
        assert r.status_code == 422
        assert provider.calls == []

    def test_via_zernio_facebook_is_refused(self, api_client, accounts, provider):
        r = _patch(api_client, accounts["facebook_zernio"].id, {"about": "Nope"})
        assert r.status_code == 422
        assert "via Zernio" in r.json()["detail"]
        assert provider.calls == []


# ---------------------------------------------------------------------------
# TikTok / Instagram — manual only
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestManualOnly:
    def test_tiktok_is_refused(self, api_client, accounts, provider):
        r = _patch(api_client, accounts["tiktok"].id, {"description": "Nope"})
        assert r.status_code == 422
        assert "manual di aplikasi TikTok/Instagram" in r.json()["detail"]
        assert provider.calls == []

    def test_instagram_via_zernio_is_refused(self, api_client, accounts, provider):
        r = _patch(api_client, accounts["instagram_zernio"].id, {"description": "Nope"})
        assert r.status_code == 422
        assert "manual di aplikasi TikTok/Instagram" in r.json()["detail"]
        assert provider.calls == []
