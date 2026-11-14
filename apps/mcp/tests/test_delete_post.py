"""MCP `delete_post` tool (v1.2.0, Agen B).

Mirrors the REST DELETE /api/v1/posts/{id} rules: any state may be
deleted except while a child is `publishing`. Handlers are called
directly with a context dict, like the live-tool tests — no YouTube
or platform call is ever made.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from django.utils import timezone

from apps.api_keys import services
from apps.composer.models import PlatformPost, Post
from apps.composer.services import create_post
from apps.mcp.handlers import _delete_post
from apps.mcp.protocol import INVALID_PARAMS, JsonRpcError
from apps.mcp.tools import get_tool, is_read_only_tool
from apps.members.models import PERMISSION_KEYS, OrgMembership, WorkspaceMembership
from apps.social_accounts.models import SocialAccount


@pytest.fixture
def user(db):
    from apps.accounts.models import User

    return User.objects.create_user(
        email="delete-mcp-owner@example.com",
        password="pw",
        name="Delete MCP Owner",
        tos_accepted_at=timezone.now(),
    )


@pytest.fixture
def organization(db):
    from apps.organizations.models import Organization

    return Organization.objects.create(name="Delete MCP Org")


@pytest.fixture
def workspace(db, organization):
    from apps.workspaces.models import Workspace

    return Workspace.objects.create(name="Delete MCP WS", organization=organization)


@pytest.fixture
def owner_memberships(db, user, organization, workspace):
    OrgMembership.objects.create(user=user, organization=organization, org_role=OrgMembership.OrgRole.OWNER)
    return WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )


@pytest.fixture
def linkedin(db, workspace):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform="linkedin_personal",
        account_platform_id="li-mcp-del-1",
        account_name="LinkedIn",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )


@pytest.fixture
def issued_key(db, user, owner_memberships, workspace, linkedin):
    return services.issue_api_key(
        workspace=workspace,
        social_accounts=[linkedin],
        issued_by=user,
        name="delete-mcp",
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


def _draft(workspace, linkedin):
    return create_post(workspace=workspace, social_account=linkedin, caption="hi", status="draft")


@pytest.mark.django_db
class TestDeletePostTool:
    def test_tool_is_registered_and_not_read_only(self):
        assert get_tool("delete_post") is not None
        assert is_read_only_tool("delete_post") is False

    def test_delete_draft(self, context, workspace, linkedin):
        post = _draft(workspace, linkedin)

        payload = _payload(_delete_post({"post_id": str(post.id)}, context))

        assert payload == {"post_id": str(post.id), "deleted": True}
        assert not Post.objects.filter(id=post.id).exists()
        assert not PlatformPost.objects.filter(post_id=post.id).exists()

    def test_delete_publishing_is_refused_and_rows_stay(self, context, workspace, linkedin):
        post = _draft(workspace, linkedin)
        PlatformPost.objects.filter(post_id=post.id).update(status="publishing")

        with pytest.raises(JsonRpcError) as excinfo:
            _delete_post({"post_id": str(post.id)}, context)

        assert excinfo.value.code == INVALID_PARAMS
        assert "currently publishing" in excinfo.value.message
        assert Post.objects.filter(id=post.id).exists()
        assert PlatformPost.objects.filter(post_id=post.id, status="publishing").exists()

    def test_delete_published_succeeds(self, context, workspace, linkedin):
        post = _draft(workspace, linkedin)
        PlatformPost.objects.filter(post_id=post.id).update(status="published")

        payload = _payload(_delete_post({"post_id": str(post.id)}, context))

        assert payload == {"post_id": str(post.id), "deleted": True}
        assert not Post.objects.filter(id=post.id).exists()

    def test_delete_unknown_post_is_not_found(self, context):
        import uuid

        with pytest.raises(JsonRpcError) as excinfo:
            _delete_post({"post_id": str(uuid.uuid4())}, context)

        assert excinfo.value.code == INVALID_PARAMS
        assert "Post not found" in excinfo.value.message

    def test_delete_without_create_posts_is_refused(self, context, workspace, linkedin):
        post = _draft(workspace, linkedin)
        context["membership"] = SimpleNamespace(effective_permissions={})

        with pytest.raises(JsonRpcError) as excinfo:
            _delete_post({"post_id": str(post.id)}, context)

        assert excinfo.value.code == INVALID_PARAMS
        assert "create_posts" in excinfo.value.message
        assert Post.objects.filter(id=post.id).exists()
