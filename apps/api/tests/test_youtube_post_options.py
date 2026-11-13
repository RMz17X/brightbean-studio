"""POST /posts — YouTube-only options (CreatePostRequest → platform_extra).

NOT RUN YET (per task rules: tests are written but not executed here).
Covers the contract in ``apps/composer/services.py::create_post``:

* supplied YouTube fields land in ``PlatformPost.platform_extra`` under
  exactly the keys the publisher engine / YouTube provider read
  (``privacy_status``, ``tags``, ``category_id``,
  ``thumbnail_asset_id``, ``self_declared_made_for_kids``);
* omitting them leaves ``platform_extra`` empty (publish-time defaults
  apply: public, no tags, no thumbnail, category "22", not made for kids);
* a thumbnail asset that is missing or belongs to another workspace → 422;
* an invalid ``privacy_status`` / out-of-cap ``tags`` → 422 (schema);
* non-YouTube destinations ignore the fields entirely;
* via Zernio only ``privacy_status`` + ``tags`` survive (no thumbnail /
  made-for-kids / category — Zernio has no such fields).
"""

from __future__ import annotations

import json
import uuid

import pytest
from django.test import Client
from django.utils import timezone

from apps.api_keys import services
from apps.composer.models import PlatformPost, Post
from apps.members.models import PERMISSION_KEYS, OrgMembership, WorkspaceMembership


# ---------------------------------------------------------------------------
# Fixtures (self-contained; mirrors apps/api/tests/test_routers.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def user(db):
    from apps.accounts.models import User

    return User.objects.create_user(
        email="yt-owner@example.com",
        password="x",
        name="YT Owner",
        tos_accepted_at=timezone.now(),
    )


@pytest.fixture
def organization(db):
    from apps.organizations.models import Organization

    return Organization.objects.create(name="YT Test Org")


@pytest.fixture
def workspace(db, organization):
    from apps.workspaces.models import Workspace

    return Workspace.objects.create(name="YT Workspace", organization=organization)


@pytest.fixture
def owner_memberships(db, user, organization, workspace):
    OrgMembership.objects.create(user=user, organization=organization, org_role=OrgMembership.OrgRole.OWNER)
    return WorkspaceMembership.objects.create(
        user=user,
        workspace=workspace,
        workspace_role=WorkspaceMembership.WorkspaceRole.OWNER,
    )


def _account(workspace, platform, **kwargs):
    from apps.social_accounts.models import SocialAccount

    return SocialAccount.objects.create(
        workspace=workspace,
        platform=platform,
        account_platform_id=kwargs.pop("account_platform_id", f"{platform}-yt-1"),
        account_name=kwargs.pop("account_name", f"{platform} YT acct"),
        connection_status=kwargs.pop("connection_status", "connected"),
        **kwargs,
    )


@pytest.fixture
def youtube_account(db, workspace):
    return _account(workspace, "youtube", account_platform_id="yt-chan-1", account_name="YT Channel")


@pytest.fixture
def linkedin_account(db, workspace):
    return _account(workspace, "linkedin_personal", account_platform_id="li-yt-1", account_name="LinkedIn")


@pytest.fixture
def zernio_youtube_account(db, workspace):
    """Zernio-transport account whose destination is YouTube."""
    from apps.zernio.models import ZernioAccountBinding, ZernioApiKey

    account = _account(workspace, "zernio", account_platform_id="z-yt-1", account_name="Zernio YT")
    key = ZernioApiKey.objects.create(workspace=workspace, label="Test key", api_key="sk_test_dummy")
    ZernioAccountBinding.objects.create(
        social_account=account,
        api_key=key,
        zernio_account_id="z-youtube",
        zernio_profile_id="prof1",
        destination_platform="youtube",
    )
    return account


def _client_for(user, owner_memberships, workspace, accounts):
    key = services.issue_api_key(
        workspace=workspace,
        social_accounts=list(accounts),
        issued_by=user,
        name="yt-options",
        permissions=list(PERMISSION_KEYS),
    )
    return _SecureClient(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")


@pytest.fixture
def yt_client(db, user, owner_memberships, workspace, youtube_account):
    return _client_for(user, owner_memberships, workspace, [youtube_account])


@pytest.fixture
def linkedin_client(db, user, owner_memberships, workspace, linkedin_account):
    return _client_for(user, owner_memberships, workspace, [linkedin_account])


@pytest.fixture
def zernio_client(db, user, owner_memberships, workspace, zernio_youtube_account):
    return _client_for(user, owner_memberships, workspace, [zernio_youtube_account])


@pytest.fixture
def thumb_asset(db, workspace):
    from apps.media_library.models import MediaAsset

    return MediaAsset.objects.create(
        organization=workspace.organization,
        workspace=workspace,
        file="test/thumb.jpg",
        filename="thumb.jpg",
        media_type=MediaAsset.MediaType.IMAGE,
        mime_type="image/jpeg",
    )


@pytest.fixture
def foreign_thumb_asset(db, organization):
    """Thumbnail-sized image living in a *different* workspace."""
    from apps.media_library.models import MediaAsset
    from apps.workspaces.models import Workspace

    other = Workspace.objects.create(name="Other WS", organization=organization)
    return MediaAsset.objects.create(
        organization=organization,
        workspace=other,
        file="test/foreign.jpg",
        filename="foreign.jpg",
        media_type=MediaAsset.MediaType.IMAGE,
        mime_type="image/jpeg",
    )


class _SecureClient(Client):
    """Django test client that forces ``secure=True`` (see test_routers.py)."""

    def generic(self, method, path, *args, **kwargs):
        kwargs["secure"] = True
        return super().generic(method, path, *args, **kwargs)


def _post(client, account, **extra):
    body = {
        "social_account_id": str(account.id),
        "caption": "YouTube options test.",
        "action": "draft",
        **extra,
    }
    return client.post("/api/v1/posts/", data=json.dumps(body), content_type="application/json")


def _platform_extra(post_id):
    return PlatformPost.objects.get(post_id=post_id).platform_extra


# ---------------------------------------------------------------------------
# Happy path + defaults
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestYouTubeOptionsStored:
    def test_full_options_land_in_platform_extra(self, yt_client, youtube_account, thumb_asset):
        r = _post(
            yt_client,
            youtube_account,
            privacy_status="unlisted",
            tags=["bean", "studio"],
            thumbnail_asset_id=str(thumb_asset.id),
            category_id="10",
            made_for_kids=True,
        )
        assert r.status_code == 201, r.content
        assert _platform_extra(r.json()["id"]) == {
            "privacy_status": "unlisted",
            "tags": ["bean", "studio"],
            "thumbnail_asset_id": str(thumb_asset.id),
            "category_id": "10",
            "self_declared_made_for_kids": True,
        }

    def test_partial_options_only_store_what_was_sent(self, yt_client, youtube_account):
        r = _post(yt_client, youtube_account, privacy_status="private")
        assert r.status_code == 201, r.content
        assert _platform_extra(r.json()["id"]) == {"privacy_status": "private"}

    def test_no_options_leaves_platform_extra_empty(self, yt_client, youtube_account):
        r = _post(yt_client, youtube_account)
        assert r.status_code == 201, r.content
        # Publish-time defaults apply downstream (public / no tags /
        # no thumbnail / category "22" / not made for kids).
        assert _platform_extra(r.json()["id"]) == {}
        assert Post.objects.count() == 1


# ---------------------------------------------------------------------------
# Validation → 422
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestYouTubeOptionsValidation:
    def test_foreign_workspace_thumbnail_is_422(self, yt_client, youtube_account, foreign_thumb_asset):
        r = _post(yt_client, youtube_account, thumbnail_asset_id=str(foreign_thumb_asset.id))
        assert r.status_code == 422
        assert Post.objects.count() == 0

    def test_missing_thumbnail_is_422(self, yt_client, youtube_account):
        r = _post(yt_client, youtube_account, thumbnail_asset_id=str(uuid.uuid4()))
        assert r.status_code == 422
        assert Post.objects.count() == 0

    def test_invalid_privacy_status_is_422(self, yt_client, youtube_account):
        r = _post(yt_client, youtube_account, privacy_status="friends_only")
        assert r.status_code == 422
        assert Post.objects.count() == 0

    def test_too_many_tags_is_422(self, yt_client, youtube_account):
        r = _post(yt_client, youtube_account, tags=[f"t{i}" for i in range(31)])
        assert r.status_code == 422

    def test_tags_over_500_chars_total_is_422(self, yt_client, youtube_account):
        # 11 tag BERBEDA @50 karakter: total 11*50 + 10 pemisah = 560 > 500.
        # Tag identik tidak bisa dipakai di sini: validator men-drop
        # duplikat DULU (sesuai kontrak), jadi ["a"*50]*11 hanya 50 karakter.
        tags = [chr(ord("a") + i) * 50 for i in range(11)]
        r = _post(yt_client, youtube_account, tags=tags)
        assert r.status_code == 422

    def test_blank_tag_is_422(self, yt_client, youtube_account):
        r = _post(yt_client, youtube_account, tags=["bean", "   "])
        assert r.status_code == 422

    def test_non_numeric_category_id_is_422(self, yt_client, youtube_account):
        r = _post(yt_client, youtube_account, category_id="People & Blogs")
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Other destinations / transports
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestYouTubeOptionsIgnoredElsewhere:
    def test_non_youtube_destination_ignores_options(self, linkedin_client, linkedin_account, thumb_asset):
        r = _post(
            linkedin_client,
            linkedin_account,
            privacy_status="unlisted",
            tags=["bean"],
            thumbnail_asset_id=str(thumb_asset.id),
            category_id="10",
            made_for_kids=True,
        )
        assert r.status_code == 201, r.content
        assert _platform_extra(r.json()["id"]) == {}

    def test_zernio_keeps_only_privacy_and_tags(
        self, zernio_client, zernio_youtube_account, thumb_asset
    ):
        r = _post(
            zernio_client,
            zernio_youtube_account,
            privacy_status="unlisted",
            tags=["bean"],
            thumbnail_asset_id=str(thumb_asset.id),
            category_id="10",
            made_for_kids=True,
        )
        assert r.status_code == 201, r.content
        # Zernio has no thumbnail / made-for-kids / category field; the
        # thumbnail is NOT validated either (it is dropped, not resolved).
        assert _platform_extra(r.json()["id"]) == {
            "privacy_status": "unlisted",
            "tags": ["bean"],
        }
