"""Tests for the Live MCP tools (live_create / live_list / live_transition / live_delete).

Handlers are called directly with a context dict (the same shape the
transport builds), and no YouTube call is ever made: the provider is
faked at the ``live_service._get_provider_for_platform`` seam and
``live_ready`` is patched.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from django.utils import timezone

from apps.api_keys import services
from apps.mcp.handlers import (
    _live_create,
    _live_delete,
    _live_list,
    _live_transition,
)
from apps.mcp.protocol import INVALID_PARAMS, JsonRpcError
from apps.mcp.tools import all_tools, get_tool, is_read_only_tool
from apps.members.models import PERMISSION_KEYS, OrgMembership, WorkspaceMembership
from apps.social_accounts.models import LiveBroadcast, SocialAccount
from apps.api.tests.test_live_router import FakeLiveProvider, LIVE_KEY


@pytest.fixture
def provider(monkeypatch):
    fake = FakeLiveProvider()
    monkeypatch.setattr(
        "apps.social_accounts.live_service._get_provider_for_platform",
        lambda platform, org_id: fake,
    )
    monkeypatch.setattr("apps.social_accounts.live_service.live_ready", lambda account: True)
    return fake


@pytest.fixture
def user(db):
    from apps.accounts.models import User

    return User.objects.create_user(
        email="live-mcp-owner@example.com",
        password="secret-123",
        name="Live MCP Owner",
        tos_accepted_at=timezone.now(),
    )


@pytest.fixture
def organization(db):
    from apps.organizations.models import Organization

    return Organization.objects.create(name="Live MCP Org")


@pytest.fixture
def workspace(db, organization):
    from apps.workspaces.models import Workspace

    return Workspace.objects.create(name="Live MCP WS", organization=organization)


@pytest.fixture
def owner_memberships(db, user, organization, workspace):
    OrgMembership.objects.create(user=user, organization=organization, org_role=OrgMembership.OrgRole.OWNER)
    return WorkspaceMembership.objects.create(
        user=user,
        workspace=workspace,
        workspace_role=WorkspaceMembership.WorkspaceRole.OWNER,
    )


@pytest.fixture
def youtube_account(db, workspace):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform="youtube",
        account_platform_id="yt-mcp-1",
        account_name="YT Channel",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )


@pytest.fixture
def foreign_account(db, organization):
    from apps.workspaces.models import Workspace

    other = Workspace.objects.create(name="Other MCP WS", organization=organization)
    return SocialAccount.objects.create(
        workspace=other,
        platform="youtube",
        account_platform_id="yt-mcp-other",
        account_name="Other YT",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )


@pytest.fixture
def issued_key(db, user, owner_memberships, workspace, youtube_account):
    return services.issue_api_key(
        workspace=workspace,
        social_accounts=[youtube_account],
        issued_by=user,
        name="live-mcp",
        permissions=list(PERMISSION_KEYS),
    )


@pytest.fixture
def context(issued_key, workspace):
    # ``issue_api_key`` returns an IssuedKey wrapper — handlers need the
    # real ApiKey row (``social_accounts``, ``workspace_id``).
    return {
        "api_key": issued_key.api_key,
        "workspace": workspace,
        "membership": SimpleNamespace(effective_permissions={"create_posts": True}),
    }


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def _broadcast(workspace, account, **kwargs):
    defaults = {
        "workspace": workspace,
        "social_account": account,
        "broadcast_id": "bcast-mcp-1",
        "stream_id": "stream-mcp-1",
        "title": "MCP Live",
        "status": LiveBroadcast.Status.READY,
        "stream_url": "rtmp://a.rtmp.youtube.com/live2",
        "stream_key": LIVE_KEY,
    }
    defaults.update(kwargs)
    return LiveBroadcast.objects.create(**defaults)


@pytest.mark.django_db
class TestRegistration:
    def test_live_tools_registered(self):
        names = {t.name for t in all_tools()}
        assert {"live_create", "live_list", "live_transition", "live_delete"} <= names
        assert get_tool("live_create") is not None
        assert is_read_only_tool("live_list") is True
        assert is_read_only_tool("live_create") is False


@pytest.mark.django_db
class TestLiveCreateTool:
    def test_create_returns_full_key_once(self, context, youtube_account, provider):
        result = _live_create(
            {"social_account_id": str(youtube_account.id), "title": "MCP Live"},
            context,
        )
        body = _payload(result)
        assert body["broadcast_id"] == "bcast-test-1"
        assert body["stream_key"] == LIVE_KEY
        assert body["masked_stream_key"] == "••••-••••-••••-dddd"
        assert body["key_shown"] is True
        assert LiveBroadcast.objects.get(broadcast_id="bcast-test-1").key_shown is True

    def test_create_requires_title(self, context, youtube_account):
        with pytest.raises(JsonRpcError) as excinfo:
            _live_create({"social_account_id": str(youtube_account.id)}, context)
        assert excinfo.value.code == INVALID_PARAMS

    def test_create_rejects_foreign_account(self, context, foreign_account):
        with pytest.raises(JsonRpcError) as excinfo:
            _live_create(
                {"social_account_id": str(foreign_account.id), "title": "Nope"},
                context,
            )
        assert excinfo.value.code == INVALID_PARAMS
        assert "allowlist" in excinfo.value.message

    def test_create_requires_create_posts(self, context):
        context["membership"] = SimpleNamespace(effective_permissions={"create_posts": False})
        with pytest.raises(JsonRpcError) as excinfo:
            _live_create({"social_account_id": "00000000-0000-0000-0000-000000000000", "title": "Nope"}, context)
        assert excinfo.value.code == INVALID_PARAMS
        assert "create_posts" in excinfo.value.message


@pytest.mark.django_db
class TestLiveListTool:
    def test_list_never_returns_full_key(self, context, workspace, youtube_account):
        _broadcast(workspace, youtube_account)
        body = _payload(_live_list({}, context))
        assert len(body["broadcasts"]) == 1
        assert body["broadcasts"][0]["stream_key"] is None
        assert LIVE_KEY not in json.dumps(body)

    def test_list_refresh_polls_active_broadcasts(self, context, workspace, youtube_account, provider):
        _broadcast(workspace, youtube_account, status=LiveBroadcast.Status.LIVE)
        body = _payload(_live_list({"refresh": True}, context))
        assert body["broadcasts"][0]["viewers"] == 42
        assert body["broadcasts"][0]["stream_status"] == "active"

    def test_list_bad_status_is_invalid_params(self, context):
        with pytest.raises(JsonRpcError) as excinfo:
            _live_list({"status": "nope"}, context)
        assert excinfo.value.code == INVALID_PARAMS


@pytest.mark.django_db
class TestLiveTransitionTool:
    def test_public_go_live_needs_confirm(self, context, workspace, youtube_account):
        row = _broadcast(workspace, youtube_account, privacy_status="public")
        with pytest.raises(JsonRpcError) as excinfo:
            _live_transition({"broadcast_id": str(row.id), "action": "go_live"}, context)
        assert excinfo.value.code == INVALID_PARAMS
        assert "confirm" in excinfo.value.message

    def test_public_go_live_with_confirm(self, context, workspace, youtube_account, provider):
        row = _broadcast(workspace, youtube_account, privacy_status="public")
        body = _payload(
            _live_transition({"broadcast_id": str(row.id), "action": "go_live", "confirm": True}, context)
        )
        assert body["status"] == "live"

    def test_stop(self, context, workspace, youtube_account, provider):
        row = _broadcast(workspace, youtube_account, status=LiveBroadcast.Status.LIVE)
        body = _payload(_live_transition({"broadcast_id": str(row.id), "action": "stop"}, context))
        assert body["status"] == "complete"

    def test_unknown_action_is_invalid_params(self, context, workspace, youtube_account):
        row = _broadcast(workspace, youtube_account)
        with pytest.raises(JsonRpcError) as excinfo:
            _live_transition({"broadcast_id": str(row.id), "action": "explode"}, context)
        assert excinfo.value.code == INVALID_PARAMS


@pytest.mark.django_db
class TestLiveDeleteTool:
    def test_delete(self, context, workspace, youtube_account, provider):
        row = _broadcast(workspace, youtube_account)
        body = _payload(_live_delete({"broadcast_id": str(row.id)}, context))
        assert body == {"id": str(row.id), "broadcast_id": "bcast-mcp-1", "deleted": True}
        assert not LiveBroadcast.objects.filter(pk=row.id).exists()
