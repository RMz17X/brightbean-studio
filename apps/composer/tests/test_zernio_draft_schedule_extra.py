"""F12 (write side): composer records draft/schedule state for via-Zernio rows.

``create_post`` writes ``platform_extra["is_draft"]`` for a draft and
``platform_extra["scheduled_for"]`` (ISO 8601 of ``scheduled_at``) for a
scheduled post — but only when the destination account publishes
through Zernio. The provider side (``providers/zernio.py
build_post_payload``) reads exactly these keys: ``is_draft`` →
``isDraft``, ``scheduled_for`` → ``scheduledFor``, neither → ``publishNow``.
Direct destinations never get the keys.

The keys must keep tracking the row afterwards, so the transitions
that move a row between draft and scheduled (service
``transition_platform_post``, the composer's chip menu, the web save,
and ``clone_post``) re-derive them via
``sync_zernio_draft_schedule_extra`` — a draft scheduled later must
stop saying ``is_draft``, or Zernio would file a post the user asked
to publish as a draft.
"""

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.composer.models import PlatformPost, Post
from apps.composer.services import clone_post, create_post, transition_platform_post
from apps.media_library.models import MediaAsset
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace
from apps.zernio.models import ZernioAccountBinding, ZernioApiKey

SK_KEY = "sk_" + "e" * 64


def _setup():
    user = User.objects.create_user(
        email="f12-owner@example.com",
        password="testpass123",
        tos_accepted_at=timezone.now(),
    )
    org = Organization.objects.create(name="F12 Org")
    workspace = Workspace.objects.create(organization=org, name="F12 WS")
    OrgMembership.objects.create(user=user, organization=org, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    return user, org, workspace


def _account(workspace, platform, name="Acc", account_platform_id=None):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform=platform,
        account_platform_id=account_platform_id or f"{platform}-1",
        account_name=name,
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )


def _zernio_account(workspace, destination):
    account = _account(workspace, "zernio", account_platform_id=f"zernio-{destination}")
    key = ZernioApiKey.objects.create(workspace=workspace, label="Key 1", api_key=SK_KEY)
    ZernioAccountBinding.objects.create(
        social_account=account,
        api_key=key,
        zernio_account_id=f"z-{destination}",
        zernio_profile_id="prof1",
        destination_platform=destination,
    )
    return SocialAccount.objects.get(pk=account.pk)


def _video_asset(org, workspace):
    # ``file`` must be truthy: ``usable_media_types`` only counts an
    # attachment whose asset has a stored file, and video-only
    # destinations refuse to schedule without one.
    return MediaAsset.objects.create(
        organization=org,
        workspace=workspace,
        filename="clip.mp4",
        media_type=MediaAsset.MediaType.VIDEO,
        file="test/clip.mp4",
        file_size=1,
        processing_status=MediaAsset.ProcessingStatus.COMPLETED,
    )


def _child(post, account):
    return PlatformPost.objects.get(post=post, social_account=account)


@pytest.mark.django_db
def test_create_draft_via_zernio_writes_is_draft():
    _user, _org, ws = _setup()
    account = _zernio_account(ws, "tiktok")
    post = create_post(workspace=ws, social_account=account, caption="hi", status="draft")
    assert _child(post, account).platform_extra == {"is_draft": True}


@pytest.mark.django_db
def test_create_scheduled_via_zernio_writes_scheduled_for():
    _user, org, ws = _setup()
    account = _zernio_account(ws, "tiktok")
    asset = _video_asset(org, ws)
    when = timezone.now() + timezone.timedelta(days=1)
    post = create_post(
        workspace=ws,
        social_account=account,
        caption="hi",
        media_asset_ids=[asset.id],
        scheduled_at=when,
        status="scheduled",
    )
    assert _child(post, account).platform_extra == {"scheduled_for": when.isoformat()}


@pytest.mark.django_db
def test_create_draft_direct_writes_no_f12_keys():
    _user, _org, ws = _setup()
    account = _account(ws, "tiktok")
    post = create_post(workspace=ws, social_account=account, caption="hi", status="draft")
    assert _child(post, account).platform_extra == {}


@pytest.mark.django_db
def test_create_scheduled_direct_youtube_writes_no_f12_keys():
    _user, org, ws = _setup()
    account = _account(ws, "youtube")
    asset = _video_asset(org, ws)
    when = timezone.now() + timezone.timedelta(days=1)
    post = create_post(
        workspace=ws,
        social_account=account,
        caption="hi",
        media_asset_ids=[asset.id],
        scheduled_at=when,
        status="scheduled",
        youtube_privacy_status="unlisted",
        youtube_tags=["berita", "rupiah"],
    )
    extra = _child(post, account).platform_extra
    assert extra["privacy_status"] == "unlisted"
    assert extra["tags"] == ["berita", "rupiah"]
    assert "is_draft" not in extra
    assert "scheduled_for" not in extra


@pytest.mark.django_db
def test_create_scheduled_via_zernio_youtube_keeps_options_plus_scheduled_for():
    _user, org, ws = _setup()
    account = _zernio_account(ws, "youtube")
    asset = _video_asset(org, ws)
    when = timezone.now() + timezone.timedelta(days=1)
    post = create_post(
        workspace=ws,
        social_account=account,
        caption="hi",
        media_asset_ids=[asset.id],
        scheduled_at=when,
        status="scheduled",
        youtube_privacy_status="unlisted",
        youtube_tags=["berita"],
    )
    assert _child(post, account).platform_extra == {
        "privacy_status": "unlisted",
        "tags": ["berita"],
        "scheduled_for": when.isoformat(),
    }


@pytest.mark.django_db
def test_create_draft_via_zernio_youtube_keeps_options_plus_is_draft():
    _user, _org, ws = _setup()
    account = _zernio_account(ws, "youtube")
    post = create_post(
        workspace=ws,
        social_account=account,
        caption="hi",
        status="draft",
        youtube_privacy_status="private",
        youtube_tags=["berita"],
    )
    assert _child(post, account).platform_extra == {
        "privacy_status": "private",
        "tags": ["berita"],
        "is_draft": True,
    }


@pytest.mark.django_db
def test_transition_draft_to_scheduled_and_back_syncs_keys():
    _user, org, ws = _setup()
    account = _zernio_account(ws, "tiktok")
    asset = _video_asset(org, ws)
    post = create_post(
        workspace=ws, social_account=account, caption="hi", media_asset_ids=[asset.id], status="draft"
    )
    pp = _child(post, account)
    assert pp.platform_extra == {"is_draft": True}

    when = timezone.now() + timezone.timedelta(days=1)
    transition_platform_post(pp, "scheduled", scheduled_at=when)
    pp.refresh_from_db()
    assert pp.platform_extra == {"scheduled_for": when.isoformat()}

    transition_platform_post(pp, "draft")
    pp.refresh_from_db()
    assert pp.platform_extra == {"is_draft": True}


@pytest.mark.django_db
def test_transition_to_publishing_drops_both_keys():
    _user, org, ws = _setup()
    account = _zernio_account(ws, "tiktok")
    asset = _video_asset(org, ws)
    when = timezone.now() + timezone.timedelta(days=1)
    post = create_post(
        workspace=ws,
        social_account=account,
        caption="hi",
        media_asset_ids=[asset.id],
        scheduled_at=when,
        status="scheduled",
    )
    pp = _child(post, account)
    assert pp.platform_extra == {"scheduled_for": when.isoformat()}
    transition_platform_post(pp, "publishing")
    pp.refresh_from_db()
    assert pp.platform_extra == {}


@pytest.mark.django_db
def test_clone_drops_stale_scheduled_for():
    _user, org, ws = _setup()
    account = _zernio_account(ws, "tiktok")
    asset = _video_asset(org, ws)
    when = timezone.now() + timezone.timedelta(days=1)
    post = create_post(
        workspace=ws,
        social_account=account,
        caption="hi",
        media_asset_ids=[asset.id],
        scheduled_at=when,
        status="scheduled",
    )
    assert _child(post, account).platform_extra == {"scheduled_for": when.isoformat()}
    copy = clone_post(post)
    assert _child(copy, account).platform_extra == {"is_draft": True}


@pytest.mark.django_db
def test_web_save_draft_writes_is_draft_for_via_zernio():
    user, _org, ws = _setup()
    account = _zernio_account(ws, "facebook")
    client = Client()
    client.force_login(user)
    save_url = reverse("composer:save_post", kwargs={"workspace_id": ws.id})
    response = client.post(
        save_url,
        data={
            "action": "save_draft",
            "title": "T",
            "caption": "body",
            "selected_accounts": str(account.id),
        },
    )
    assert response.status_code in (200, 204, 302), response.content[:500]
    post = Post.objects.filter(workspace=ws).order_by("-created_at").first()
    assert post is not None
    assert _child(post, account).platform_extra == {"is_draft": True}


@pytest.mark.django_db
def test_web_chip_schedule_drops_is_draft():
    user, org, ws = _setup()
    account = _zernio_account(ws, "tiktok")
    asset = _video_asset(org, ws)
    post = create_post(
        workspace=ws, social_account=account, caption="hi", media_asset_ids=[asset.id], status="draft"
    )
    pp = _child(post, account)
    assert pp.platform_extra == {"is_draft": True}
    client = Client()
    client.force_login(user)
    url = reverse(
        "composer:transition_platform_post",
        kwargs={"workspace_id": ws.id, "post_id": post.id, "platform_post_id": pp.id},
    )
    response = client.post(url, data={"target_status": "scheduled"})
    assert response.status_code == 200, response.content[:500]
    pp.refresh_from_db()
    assert pp.status == "scheduled"
    # No scheduled time anywhere on the row or the parent, so there is
    # no scheduled_for to write — but the stale is_draft must be gone,
    # or the engine would hand Zernio a draft for a scheduled row.
    assert "is_draft" not in (pp.platform_extra or {})
    assert "scheduled_for" not in (pp.platform_extra or {})
