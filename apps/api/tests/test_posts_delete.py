"""DELETE /api/v1/posts/{id} (v1.2.0, Agen B).

Covers §5.3 of the media/delete design doc: every state deletable
except `publishing` (422), ambiguous 404s, permission gate, audit row,
second DELETE → 404, and cascade behaviour (children/logs/links/
versions/queue entries gone, MediaAsset rows survive).
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from apps.api_keys import services
from apps.api_keys.models import ApiKeyAuditLog
from apps.calendar.models import Queue, QueueEntry
from apps.composer.models import PlatformPost, Post, PostMedia, PostVersion
from apps.composer.services import create_post
from apps.media_library.models import MediaAsset
from apps.members.models import PERMISSION_KEYS, OrgMembership, WorkspaceMembership
from apps.publisher.models import PublishLog
from apps.social_accounts.models import SocialAccount


class _SecureClient(Client):
    def generic(self, method, path, *args, **kwargs):
        kwargs["secure"] = True
        return super().generic(method, path, *args, **kwargs)


@pytest.fixture
def user(db):
    from apps.accounts.models import User

    return User.objects.create_user(
        email="delete-owner@example.com",
        password="pw",
        name="Delete Owner",
        tos_accepted_at=timezone.now(),
    )


@pytest.fixture
def organization(db):
    from apps.organizations.models import Organization

    return Organization.objects.create(name="Delete Org")


@pytest.fixture
def workspace(db, organization, user):
    from apps.workspaces.models import Workspace

    workspace = Workspace.objects.create(name="Delete WS", organization=organization)
    OrgMembership.objects.create(user=user, organization=organization, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    return workspace


def _account(workspace, platform):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform=platform,
        account_platform_id=f"{platform}-del-1",
        account_name=platform,
        connection_status="connected",
    )


@pytest.fixture
def linkedin(workspace):
    return _account(workspace, "linkedin_personal")


@pytest.fixture
def tiktok(workspace):
    return _account(workspace, "tiktok")


@pytest.fixture
def client(user, workspace, linkedin, tiktok):
    key = services.issue_api_key(
        workspace=workspace,
        social_accounts=[linkedin, tiktok],
        issued_by=user,
        name="delete-posts",
        permissions=list(PERMISSION_KEYS),
    )
    return _SecureClient(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")


def _asset(workspace, media_type):
    return MediaAsset.objects.create(
        organization=workspace.organization,
        workspace=workspace,
        file=f"test/{media_type}",
        filename=media_type,
        media_type=media_type,
        mime_type="video/mp4" if media_type == MediaAsset.MediaType.VIDEO else "image/jpeg",
    )


def _when():
    return (timezone.now() + timedelta(hours=2)).isoformat()


def _create(client, account, *, action="draft", media=()):
    body = {
        "social_account_id": str(account.id),
        "caption": "hi",
        "action": action,
        "media_asset_ids": [str(asset.id) for asset in media],
    }
    if action == "schedule":
        body["scheduled_at"] = _when()
    return client.post("/api/v1/posts/", data=json.dumps(body), content_type="application/json")


def _delete(client, post_id):
    return client.delete(f"/api/v1/posts/{post_id}")


def _set_child_status(post_id, status):
    PlatformPost.objects.filter(post_id=post_id).update(status=status)


@pytest.mark.django_db
class TestDeletePost:
    def test_delete_draft_cascades_but_keeps_media_asset(self, client, workspace, linkedin):
        video = _asset(workspace, MediaAsset.MediaType.VIDEO)
        post_id = _create(client, linkedin, media=[video]).json()["id"]
        post = Post.objects.get(id=post_id)
        child = post.platform_posts.get()
        PublishLog.objects.create(platform_post=child, attempt_number=1, status_code=200)
        PostVersion.objects.create(post=post, version_number=1, snapshot={"caption": "hi"})
        queue = Queue.objects.create(workspace=workspace, name="Q", social_account=linkedin)
        QueueEntry.objects.create(queue=queue, post=post)

        response = _delete(client, post_id)

        assert response.status_code == 200, response.content
        assert response.json() == {"id": post_id, "deleted": True}
        assert not Post.objects.filter(id=post_id).exists()
        assert not PlatformPost.objects.filter(post_id=post_id).exists()
        assert not PublishLog.objects.filter(platform_post__post_id=post_id).exists()
        assert not PostMedia.objects.filter(post_id=post_id).exists()
        assert not PostVersion.objects.filter(post_id=post_id).exists()
        assert not QueueEntry.objects.filter(post_id=post_id).exists()
        # Only the link row is gone — the asset itself survives for the
        # orphaned-media sweep (min. age 14 days).
        assert MediaAsset.objects.filter(id=video.id).exists()

    def test_delete_scheduled(self, client, linkedin):
        post_id = _create(client, linkedin, action="schedule").json()["id"]

        response = _delete(client, post_id)

        assert response.status_code == 200, response.content
        assert not Post.objects.filter(id=post_id).exists()

    def test_delete_published_is_deliberate_record_only(self, client, linkedin):
        # Published content STAYS LIVE on the platform; only Bean's record
        # goes away. This 200 is the intended behaviour, not a bug.
        post_id = _create(client, linkedin, action="schedule").json()["id"]
        _set_child_status(post_id, "published")

        response = _delete(client, post_id)

        assert response.status_code == 200, response.content
        assert not Post.objects.filter(id=post_id).exists()

    def test_delete_publishing_is_refused_and_rows_stay(self, client, linkedin):
        post_id = _create(client, linkedin, action="schedule").json()["id"]
        _set_child_status(post_id, "publishing")

        response = _delete(client, post_id)

        assert response.status_code == 422, response.content
        assert response.json()["error"] == "unprocessable_entity"
        assert "currently publishing" in response.json()["detail"]
        assert Post.objects.filter(id=post_id).exists()
        assert PlatformPost.objects.filter(post_id=post_id, status="publishing").exists()

    def test_delete_unknown_id_is_404(self, client):
        response = _delete(client, uuid.uuid4())

        assert response.status_code == 404, response.content

    def test_delete_post_in_another_workspace_is_404(self, client, user, organization):
        from apps.workspaces.models import Workspace

        other_ws = Workspace.objects.create(name="Other WS", organization=organization)
        other_account = _account(other_ws, "linkedin_personal")
        other_post = create_post(
            workspace=other_ws, social_account=other_account, caption="other", status="draft"
        )

        response = _delete(client, other_post.id)

        assert response.status_code == 404, response.content
        assert Post.objects.filter(id=other_post.id).exists()

    def test_delete_post_with_child_outside_allowlist_is_404(
        self, client, user, workspace, linkedin
    ):
        # Same workspace, but the post's only child targets an account this
        # key was never allowlisted for — must not leak its existence.
        outside = _account(workspace, "facebook")
        hidden = create_post(
            workspace=workspace, social_account=outside, caption="hidden", status="draft"
        )

        response = _delete(client, hidden.id)

        assert response.status_code == 404, response.content
        assert Post.objects.filter(id=hidden.id).exists()

    def test_delete_without_create_posts_is_403(self, user, workspace, linkedin, tiktok):
        key = services.issue_api_key(
            workspace=workspace,
            social_accounts=[linkedin, tiktok],
            issued_by=user,
            name="delete-no-perm",
            permissions=[p for p in PERMISSION_KEYS if p != "create_posts"],
        )
        no_perm = _SecureClient(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")
        post = create_post(
            workspace=workspace, social_account=linkedin, caption="hi", status="draft"
        )

        response = _delete(no_perm, post.id)

        assert response.status_code == 403, response.content

    def test_second_delete_is_404(self, client, linkedin):
        post_id = _create(client, linkedin).json()["id"]

        assert _delete(client, post_id).status_code == 200

        assert _delete(client, post_id).status_code == 404

    def test_delete_writes_audit_log(self, client, linkedin):
        post_id = _create(client, linkedin).json()["id"]

        response = _delete(client, post_id)

        assert response.status_code == 200, response.content
        assert ApiKeyAuditLog.objects.filter(
            action="post.delete", target_id=post_id
        ).exists()
