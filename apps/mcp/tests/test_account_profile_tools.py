"""Tests for the account_update_profile MCP tool.

Handlers are called directly with a context dict (the same shape the
transport builds), and no platform call is ever made: the provider is
faked at the ``profile_service._get_provider_for_platform`` seam,
reusing the ``FakeProfileProvider`` from the REST profile-router tests
so both surfaces pin the same provider contract.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from django.utils import timezone

from apps.api.tests.test_account_profile_router import FakeProfileProvider
from apps.api_keys import services
from apps.mcp.handlers import _account_update_profile
from apps.mcp.protocol import INVALID_PARAMS, JsonRpcError
from apps.mcp.tools import all_tools, get_tool, is_read_only_tool
from apps.members.models import PERMISSION_KEYS, OrgMembership, WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.zernio.models import ZernioAccountBinding, ZernioApiKey

SK_KEY = "sk_" + "z" * 64


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
        email="profile-mcp-owner@example.com",
        password="secret-123",
        name="Profile MCP Owner",
        tos_accepted_at=timezone.now(),
    )


@pytest.fixture
def organization(db):
    from apps.organizations.models import Organization

    return Organization.objects.create(name="Profile MCP Org")


@pytest.fixture
def workspace(db, organization):
    from apps.workspaces.models import Workspace

    return Workspace.objects.create(name="Profile MCP WS", organization=organization)


@pytest.fixture
def owner_memberships(db, user, organization, workspace):
    OrgMembership.objects.create(user=user, organization=organization, org_role=OrgMembership.OrgRole.OWNER)
    return WorkspaceMembership.objects.create(
        user=user,
        workspace=workspace,
        workspace_role=WorkspaceMembership.WorkspaceRole.OWNER,
    )


def _account(db, workspace, platform, name, account_platform_id):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform=platform,
        account_platform_id=account_platform_id,
        account_name=name,
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )


@pytest.fixture
def youtube_account(db, workspace):
    return _account(db, workspace, "youtube", "YT Channel", "yt-prof-1")


@pytest.fixture
def facebook_account(db, workspace):
    return _account(db, workspace, "facebook", "FB Page", "fb-prof-1")


@pytest.fixture
def tiktok_account(db, workspace):
    return _account(db, workspace, "tiktok", "TT Account", "tt-prof-1")


@pytest.fixture
def zernio_youtube_account(db, workspace):
    account = _account(db, workspace, "zernio", "YT via Zernio", "zernio-yt-prof-1")
    key = ZernioApiKey.objects.create(workspace=workspace, label="Key 1", api_key=SK_KEY)
    ZernioAccountBinding.objects.create(
        social_account=account,
        api_key=key,
        zernio_account_id="z-yt",
        zernio_profile_id="prof1",
        destination_platform="youtube",
    )
    return SocialAccount.objects.get(pk=account.pk)


@pytest.fixture
def foreign_account(db, organization):
    from apps.workspaces.models import Workspace

    other = Workspace.objects.create(name="Other Profile MCP WS", organization=organization)
    return _account(db, other, "youtube", "Other YT", "yt-prof-other")


@pytest.fixture
def issued_key(db, user, owner_memberships, workspace, youtube_account, facebook_account, tiktok_account, zernio_youtube_account):
    return services.issue_api_key(
        workspace=workspace,
        social_accounts=[youtube_account, facebook_account, tiktok_account, zernio_youtube_account],
        issued_by=user,
        name="profile-mcp",
        permissions=list(PERMISSION_KEYS),
    )


@pytest.fixture
def context(issued_key, workspace):
    return {
        "api_key": issued_key.api_key,
        "workspace": workspace,
        "membership": SimpleNamespace(effective_permissions={"create_posts": True}),
    }


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


@pytest.mark.django_db
class TestRegistration:
    def test_account_update_profile_registered(self):
        names = {t.name for t in all_tools()}
        assert "account_update_profile" in names
        assert get_tool("account_update_profile") is not None
        assert is_read_only_tool("account_update_profile") is False


@pytest.mark.django_db
class TestAccountUpdateProfileTool:
    def test_youtube_description_updated(self, context, youtube_account, provider):
        body = _payload(
            _account_update_profile(
                {"social_account_id": str(youtube_account.id), "description": "New channel bio"},
                context,
            )
        )
        assert body["id"] == str(youtube_account.id)
        assert body["platform"] == "youtube"
        assert body["transport"] is None
        assert body["description"] == "New channel bio"
        assert body["updated_fields"] == ["description"]
        assert provider.calls == [
            ("update_channel_description", "", "New channel bio", "yt-prof-1")
        ]

    def test_youtube_description_too_long_is_refused(self, context, youtube_account, provider):
        with pytest.raises(JsonRpcError) as excinfo:
            _account_update_profile(
                {"social_account_id": str(youtube_account.id), "description": "x" * 1001},
                context,
            )
        assert excinfo.value.code == INVALID_PARAMS
        assert provider.calls == []

    def test_facebook_page_info_updated(self, context, facebook_account, provider):
        body = _payload(
            _account_update_profile(
                {
                    "social_account_id": str(facebook_account.id),
                    "about": "Short about",
                    "website": "https://example.com",
                },
                context,
            )
        )
        assert body["platform"] == "facebook"
        assert body["about"] == "Short about"
        assert body["website"] == "https://example.com"
        assert body["updated_fields"] == ["about", "website"]
        assert provider.calls == [
            ("update_page_info", "", "fb-prof-1", "Short about", None, "https://example.com", None)
        ]

    def test_facebook_bad_website_is_refused(self, context, facebook_account, provider):
        with pytest.raises(JsonRpcError) as excinfo:
            _account_update_profile(
                {"social_account_id": str(facebook_account.id), "website": "not-a-url"},
                context,
            )
        assert excinfo.value.code == INVALID_PARAMS
        assert provider.calls == []

    def test_via_zernio_youtube_is_refused(self, context, zernio_youtube_account, provider):
        with pytest.raises(JsonRpcError) as excinfo:
            _account_update_profile(
                {"social_account_id": str(zernio_youtube_account.id), "description": "Nope"},
                context,
            )
        assert excinfo.value.code == INVALID_PARAMS
        assert "Zernio" in excinfo.value.message
        assert provider.calls == []

    def test_tiktok_is_refused(self, context, tiktok_account, provider):
        with pytest.raises(JsonRpcError) as excinfo:
            _account_update_profile(
                {"social_account_id": str(tiktok_account.id), "description": "Nope"},
                context,
            )
        assert excinfo.value.code == INVALID_PARAMS
        assert provider.calls == []

    def test_no_fields_is_refused(self, context, youtube_account, provider):
        with pytest.raises(JsonRpcError) as excinfo:
            _account_update_profile({"social_account_id": str(youtube_account.id)}, context)
        assert excinfo.value.code == INVALID_PARAMS
        assert provider.calls == []

    def test_blank_only_fields_are_refused(self, context, youtube_account, provider):
        with pytest.raises(JsonRpcError) as excinfo:
            _account_update_profile(
                {"social_account_id": str(youtube_account.id), "description": "   "},
                context,
            )
        assert excinfo.value.code == INVALID_PARAMS
        assert provider.calls == []

    def test_scope_failure_maps_to_invalid_params(self, context, youtube_account, provider):
        provider.fail_scope = True
        with pytest.raises(JsonRpcError) as excinfo:
            _account_update_profile(
                {"social_account_id": str(youtube_account.id), "description": "New bio"},
                context,
            )
        assert excinfo.value.code == INVALID_PARAMS

    def test_rejects_foreign_account(self, context, foreign_account):
        with pytest.raises(JsonRpcError) as excinfo:
            _account_update_profile(
                {"social_account_id": str(foreign_account.id), "description": "Nope"},
                context,
            )
        assert excinfo.value.code == INVALID_PARAMS
        assert "allowlist" in excinfo.value.message

    def test_requires_create_posts(self, context, youtube_account):
        context["membership"] = SimpleNamespace(effective_permissions={"create_posts": False})
        with pytest.raises(JsonRpcError) as excinfo:
            _account_update_profile(
                {"social_account_id": str(youtube_account.id), "description": "Nope"},
                context,
            )
        assert excinfo.value.code == INVALID_PARAMS
        assert "create_posts" in excinfo.value.message
