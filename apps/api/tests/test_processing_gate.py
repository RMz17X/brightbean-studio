"""REST surface of the `processing_status` gate (v1.2.0, Agen B).

A schedule request (create-with-schedule, or promoting an existing
draft) whose attached media is still processing answers 422 with a
clear message; once the asset reports `completed` the same request
succeeds. Draft creation is never gated — only the step that hands
the post to the publisher.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from apps.api_keys import services
from apps.composer.models import Post
from apps.media_library.models import MediaAsset
from apps.members.models import PERMISSION_KEYS, OrgMembership, WorkspaceMembership
from apps.social_accounts.models import SocialAccount


class _SecureClient(Client):
    def generic(self, method, path, *args, **kwargs):
        kwargs["secure"] = True
        return super().generic(method, path, *args, **kwargs)


@pytest.fixture
def user(db):
    from apps.accounts.models import User

    return User.objects.create_user(
        email="gate-owner@example.com",
        password="pw",
        name="Gate Owner",
        tos_accepted_at=timezone.now(),
    )


@pytest.fixture
def organization(db):
    from apps.organizations.models import Organization

    return Organization.objects.create(name="Gate Org")


@pytest.fixture
def workspace(db, organization, user):
    from apps.workspaces.models import Workspace

    workspace = Workspace.objects.create(name="Gate WS", organization=organization)
    OrgMembership.objects.create(user=user, organization=organization, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    return workspace


@pytest.fixture
def tiktok(workspace):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform="tiktok",
        account_platform_id="tiktok-gate-1",
        account_name="tiktok",
        connection_status="connected",
    )


@pytest.fixture
def client(user, workspace, tiktok):
    key = services.issue_api_key(
        workspace=workspace,
        social_accounts=[tiktok],
        issued_by=user,
        name="processing-gate",
        permissions=list(PERMISSION_KEYS),
    )
    return _SecureClient(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")


def _asset(workspace, *, processing_status=None):
    kwargs = {}
    if processing_status is not None:
        kwargs["processing_status"] = processing_status
    return MediaAsset.objects.create(
        organization=workspace.organization,
        workspace=workspace,
        file="test/video",
        filename="video",
        media_type=MediaAsset.MediaType.VIDEO,
        mime_type="video/mp4",
        **kwargs,
    )


def _when():
    return (timezone.now() + timedelta(hours=2)).isoformat()


def _create(client, account, *, action="schedule", media=()):
    body = {
        "social_account_id": str(account.id),
        "caption": "hi",
        "action": action,
        "media_asset_ids": [str(asset.id) for asset in media],
    }
    if action == "schedule":
        body["scheduled_at"] = _when()
    return client.post("/api/v1/posts/", data=json.dumps(body), content_type="application/json")


@pytest.mark.django_db
class TestProcessingGateRest:
    def test_schedule_with_pending_video_is_refused(self, client, workspace, tiktok):
        video = _asset(workspace, processing_status=MediaAsset.ProcessingStatus.PENDING)

        response = _create(client, tiktok, media=[video])

        assert response.status_code == 422, response.content
        assert "still processing" in response.json()["detail"]
        assert not Post.objects.exists()

    def test_schedule_with_completed_video_is_accepted(self, client, workspace, tiktok):
        video = _asset(workspace, processing_status=MediaAsset.ProcessingStatus.COMPLETED)

        response = _create(client, tiktok, media=[video])

        assert response.status_code == 201, response.content

    def test_draft_promotion_waits_for_processing_then_succeeds(self, client, workspace, tiktok):
        video = _asset(workspace, processing_status=MediaAsset.ProcessingStatus.PENDING)
        draft = _create(client, tiktok, action="draft", media=[video])
        assert draft.status_code == 201, draft.content
        post_id = draft.json()["id"]

        response = client.post(
            f"/api/v1/posts/{post_id}/schedule",
            data=json.dumps({"scheduled_at": _when()}),
            content_type="application/json",
        )

        assert response.status_code == 422, response.content
        assert "still processing" in response.json()["detail"]

        MediaAsset.objects.filter(pk=video.pk).update(
            processing_status=MediaAsset.ProcessingStatus.COMPLETED
        )
        response = client.post(
            f"/api/v1/posts/{post_id}/schedule",
            data=json.dumps({"scheduled_at": _when()}),
            content_type="application/json",
        )

        assert response.status_code == 200, response.content
