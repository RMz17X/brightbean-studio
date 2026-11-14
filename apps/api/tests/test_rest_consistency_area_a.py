"""REST consistency (area A): list pagination, PATCH YouTube options, error envelope.

NOT RUN YET (per task rules: tests are written but not executed here).
Covers the area-A standardisation:

* ``GET /posts/`` list — cursor + limit pagination, ``status`` /
  ``social_account_id`` filters, allowlist visibility (a post whose any
  child is outside the key's allowlist is never listed), bad cursor → 422.
* ``GET /accounts/`` pagination — ``limit`` / ``next_cursor`` on the
  allowlist-bounded list.
* ``PATCH /posts/{id}`` YouTube fields — draft + scheduled children accept
  them (the publisher reads ``platform_extra`` at publish time), published
  is 409, a post with no YouTube child is 422, a foreign thumbnail is 422,
  Zernio-unsupported fields (category / made-for-kids / thumbnail) are 422
  on a Zernio-routed YouTube child, ``tags: []`` clears, ``null`` is a no-op.
* Error envelope — 404 / 422-validation / 401 all answer
  ``{"error": "<slug>", "detail": "<str>"}``.
* Idempotency — the body ``idempotency_key`` wins over the
  ``Idempotency-Key`` header (proven by a header-only retry creating a
  second post instead of replaying/422ing), and a body-key replay returns
  the first response verbatim.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from apps.api_keys import services
from apps.composer.models import PlatformPost
from apps.members.models import PERMISSION_KEYS, OrgMembership, WorkspaceMembership

# ---------------------------------------------------------------------------
# Fixtures (self-contained; mirrors apps/api/tests/test_youtube_post_options.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def user(db):
    from apps.accounts.models import User

    return User.objects.create_user(
        email="area-a-owner@example.com",
        password="test-password-123",
        name="Area A Owner",
        tos_accepted_at=timezone.now(),
    )


@pytest.fixture
def organization(db):
    from apps.organizations.models import Organization

    return Organization.objects.create(name="Area A Test Org")


@pytest.fixture
def workspace(db, organization):
    from apps.workspaces.models import Workspace

    return Workspace.objects.create(name="Area A Workspace", organization=organization)


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
        account_platform_id=kwargs.pop("account_platform_id", f"{platform}-a1"),
        account_name=kwargs.pop("account_name", f"{platform} acct"),
        connection_status=kwargs.pop("connection_status", "connected"),
        **kwargs,
    )


@pytest.fixture
def youtube_account(db, workspace):
    return _account(workspace, "youtube", account_platform_id="yt-a1", account_name="YT Channel")


@pytest.fixture
def linkedin_account(db, workspace):
    return _account(workspace, "linkedin_personal", account_platform_id="li-a1", account_name="LinkedIn")


@pytest.fixture
def zernio_youtube_account(db, workspace):
    """Zernio-transport account whose destination is YouTube."""
    from apps.zernio.models import ZernioAccountBinding, ZernioApiKey

    account = _account(workspace, "zernio", account_platform_id="z-yt-a1", account_name="Zernio YT")
    key = ZernioApiKey.objects.create(workspace=workspace, label="Test key", api_key="test-api-key-value")
    ZernioAccountBinding.objects.create(
        social_account=account,
        api_key=key,
        zernio_account_id="z-youtube",
        zernio_profile_id="prof1",
        destination_platform="youtube",
    )
    return account


@pytest.fixture
def foreign_account(db, organization):
    """Account in a *different* workspace inside the same org."""
    from apps.social_accounts.models import SocialAccount
    from apps.workspaces.models import Workspace

    other = Workspace.objects.create(name="Other WS", organization=organization)
    return SocialAccount.objects.create(
        workspace=other,
        platform="linkedin_personal",
        account_platform_id="li-foreign",
        account_name="Foreign LinkedIn",
        connection_status="connected",
    )


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

    other = Workspace.objects.create(name="Other WS 2", organization=organization)
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


def _client_for(user, owner_memberships, workspace, accounts):
    key = services.issue_api_key(
        workspace=workspace,
        social_accounts=list(accounts),
        issued_by=user,
        name="area-a",
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
def multi_client(db, user, owner_memberships, workspace, youtube_account, linkedin_account, zernio_youtube_account):
    return _client_for(
        user, owner_memberships, workspace, [youtube_account, linkedin_account, zernio_youtube_account]
    )


def _post(client, account, **extra):
    body = {
        "social_account_id": str(account.id),
        "caption": "Area A test post.",
        "action": "draft",
        **extra,
    }
    return client.post("/api/v1/posts/", data=json.dumps(body), content_type="application/json")


def _patch(client, post_id, **fields):
    return client.patch(
        f"/api/v1/posts/{post_id}",
        data=json.dumps(fields),
        content_type="application/json",
    )


def _platform_extra(post_id):
    return PlatformPost.objects.get(post_id=post_id).platform_extra


def _set_child_status(post_id, status):
    pp = PlatformPost.objects.get(post_id=post_id)
    pp.status = status
    if status == "scheduled":
        pp.scheduled_at = timezone.now() + timedelta(hours=1)
    if status == "published":
        pp.published_at = timezone.now()
    pp.save(update_fields=["status", "scheduled_at", "published_at"])
    return pp


# ---------------------------------------------------------------------------
# GET /posts/ — list + pagination
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestPostsList:
    def test_empty_list(self, yt_client):
        r = yt_client.get("/api/v1/posts/")
        assert r.status_code == 200, r.content
        body = r.json()
        assert body == {"posts": [], "limit": 50, "next_cursor": None}

    def test_list_pagination_newest_first(self, yt_client, youtube_account):
        ids = []
        for i in range(3):
            r = _post(yt_client, youtube_account, caption=f"Post {i}.")
            assert r.status_code == 201, r.content
            ids.append(r.json()["id"])
        r = yt_client.get("/api/v1/posts/?limit=2")
        assert r.status_code == 200, r.content
        body = r.json()
        assert body["limit"] == 2
        assert body["next_cursor"]
        # Newest first: reverse creation order.
        assert [p["id"] for p in body["posts"]] == list(reversed(ids))[:2]
        r2 = yt_client.get(f"/api/v1/posts/?limit=2&cursor={body['next_cursor']}")
        assert r2.status_code == 200, r2.content
        body2 = r2.json()
        assert [p["id"] for p in body2["posts"]] == [ids[0]]
        assert body2["next_cursor"] is None

    def test_list_status_filter(self, yt_client, youtube_account):
        r = _post(yt_client, youtube_account)
        assert r.status_code == 201, r.content
        post_id = r.json()["id"]
        assert yt_client.get("/api/v1/posts/?status=draft").json()["posts"]
        assert yt_client.get("/api/v1/posts/?status=published").json()["posts"] == []
        _set_child_status(post_id, "scheduled")
        assert [p["id"] for p in yt_client.get("/api/v1/posts/?status=scheduled").json()["posts"]] == [
            post_id
        ]

    def test_list_invalid_status_is_422(self, yt_client):
        r = yt_client.get("/api/v1/posts/?status=nope")
        assert r.status_code == 422
        assert r.json()["error"] == "unprocessable_entity"

    def test_list_bad_cursor_is_422(self, yt_client):
        r = yt_client.get("/api/v1/posts/?cursor=!!!not-a-cursor!!!")
        assert r.status_code == 422
        assert r.json()["error"] == "unprocessable_entity"

    def test_list_social_account_filter(self, yt_client, youtube_account, linkedin_account):
        r = _post(yt_client, youtube_account)
        assert r.status_code == 201, r.content
        got = yt_client.get(f"/api/v1/posts/?social_account_id={youtube_account.id}").json()
        assert [p["id"] for p in got["posts"]] == [r.json()["id"]]
        # An account outside the key's allowlist is 403, never an empty page.
        r = yt_client.get(f"/api/v1/posts/?social_account_id={linkedin_account.id}")
        assert r.status_code == 403

    def test_list_hides_post_with_foreign_child(self, yt_client, youtube_account, foreign_account, workspace):
        from apps.composer.models import PlatformPost, Post

        # A post whose only child targets an account outside the key's
        # allowlist: invisible in the list (and 404 on detail, same rule).
        # Built via the ORM, not create_post: create_post rightly refuses
        # accounts outside the workspace, so the foreign child must be
        # constructed directly.
        foreign_post = Post.objects.create(workspace=workspace, caption="Foreign post.")
        PlatformPost.objects.create(post=foreign_post, social_account=foreign_account, status="draft")
        own = _post(yt_client, youtube_account)
        assert own.status_code == 201, own.content
        ids = {p["id"] for p in yt_client.get("/api/v1/posts/").json()["posts"]}
        assert ids == {own.json()["id"]}
        assert str(foreign_post.id) not in ids
        assert yt_client.get(f"/api/v1/posts/{foreign_post.id}").status_code == 404


# ---------------------------------------------------------------------------
# PATCH /posts/{id} — YouTube fields
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestPatchYouTubeOptions:
    def test_patch_draft_updates_extra_and_response(self, yt_client, youtube_account, thumb_asset):
        r = _post(yt_client, youtube_account, privacy_status="private", tags=["old"])
        assert r.status_code == 201, r.content
        post_id = r.json()["id"]
        r = _patch(
            yt_client,
            post_id,
            privacy_status="unlisted",
            tags=["bean", "studio"],
            thumbnail_asset_id=str(thumb_asset.id),
            category_id="10",
            made_for_kids=True,
        )
        assert r.status_code == 200, r.content
        assert _platform_extra(post_id) == {
            "privacy_status": "unlisted",
            "tags": ["bean", "studio"],
            "thumbnail_asset_id": str(thumb_asset.id),
            "category_id": "10",
            "self_declared_made_for_kids": True,
        }
        child = r.json()["platform_posts"][0]
        assert child["youtube_privacy_status"] == "unlisted"
        assert child["youtube_tags"] == ["bean", "studio"]
        assert child["youtube_thumbnail_asset_id"] == str(thumb_asset.id)
        assert child["youtube_category_id"] == "10"
        assert child["youtube_made_for_kids"] is True

    def test_patch_scheduled_child_ok(self, yt_client, youtube_account):
        r = _post(yt_client, youtube_account)
        assert r.status_code == 201, r.content
        post_id = r.json()["id"]
        _set_child_status(post_id, "scheduled")
        r = _patch(yt_client, post_id, privacy_status="private")
        assert r.status_code == 200, r.content
        assert _platform_extra(post_id) == {"privacy_status": "private"}

    def test_patch_published_is_409(self, yt_client, youtube_account):
        r = _post(yt_client, youtube_account)
        assert r.status_code == 201, r.content
        post_id = r.json()["id"]
        _set_child_status(post_id, "published")
        r = _patch(yt_client, post_id, privacy_status="private")
        assert r.status_code == 409
        assert _platform_extra(post_id) == {}

    def test_patch_non_youtube_is_422(self, linkedin_client, linkedin_account):
        r = _post(linkedin_client, linkedin_account)
        assert r.status_code == 201, r.content
        post_id = r.json()["id"]
        r = _patch(linkedin_client, post_id, privacy_status="private", tags=["bean"])
        assert r.status_code == 422
        assert _platform_extra(post_id) == {}

    def test_patch_foreign_thumbnail_is_422(self, yt_client, youtube_account, foreign_thumb_asset):
        r = _post(yt_client, youtube_account)
        assert r.status_code == 201, r.content
        post_id = r.json()["id"]
        r = _patch(yt_client, post_id, thumbnail_asset_id=str(foreign_thumb_asset.id))
        assert r.status_code == 422
        assert _platform_extra(post_id) == {}

    def test_patch_zernio_category_is_422_but_privacy_ok(self, zernio_client, zernio_youtube_account):
        r = _post(zernio_client, zernio_youtube_account)
        assert r.status_code == 201, r.content
        post_id = r.json()["id"]
        r = _patch(zernio_client, post_id, category_id="10")
        assert r.status_code == 422
        r = _patch(zernio_client, post_id, made_for_kids=True)
        assert r.status_code == 422
        # NOTE: exact-dict equality is wrong here — a via-Zernio draft may
        # also carry transport keys in platform_extra (e.g. the F12
        # ``is_draft`` key), which PATCH must preserve, not wipe. Assert on
        # the YouTube keys only, plus preservation of pre-existing keys.
        before = _platform_extra(post_id)
        r = _patch(zernio_client, post_id, privacy_status="unlisted", tags=["bean"])
        assert r.status_code == 200, r.content
        extra = _platform_extra(post_id)
        assert extra["privacy_status"] == "unlisted"
        assert extra["tags"] == ["bean"]
        assert "category_id" not in extra
        assert "self_declared_made_for_kids" not in extra
        assert "thumbnail_asset_id" not in extra
        for key, value in before.items():
            assert extra.get(key) == value

    def test_patch_tags_empty_list_clears(self, yt_client, youtube_account):
        r = _post(yt_client, youtube_account, tags=["bean", "studio"])
        assert r.status_code == 201, r.content
        post_id = r.json()["id"]
        r = _patch(yt_client, post_id, tags=[])
        assert r.status_code == 200, r.content
        assert _platform_extra(post_id) == {}
        assert r.json()["platform_posts"][0]["youtube_tags"] == []

    def test_patch_tags_over_cap_is_422(self, yt_client, youtube_account):
        r = _post(yt_client, youtube_account)
        assert r.status_code == 201, r.content
        post_id = r.json()["id"]
        r = _patch(yt_client, post_id, tags=[f"t{i}" for i in range(31)])
        assert r.status_code == 422

    def test_patch_null_is_noop(self, yt_client, youtube_account):
        r = _post(yt_client, youtube_account, privacy_status="private")
        assert r.status_code == 201, r.content
        post_id = r.json()["id"]
        r = _patch(yt_client, post_id, privacy_status=None, caption="New caption.")
        assert r.status_code == 200, r.content
        assert r.json()["caption"] == "New caption."
        assert _platform_extra(post_id) == {"privacy_status": "private"}

    def test_create_response_exposes_youtube_fields(self, yt_client, youtube_account):
        r = _post(yt_client, youtube_account, privacy_status="unlisted", tags=["bean"])
        assert r.status_code == 201, r.content
        child = r.json()["platform_posts"][0]
        assert child["youtube_privacy_status"] == "unlisted"
        assert child["youtube_tags"] == ["bean"]
        assert child["youtube_thumbnail_asset_id"] is None
        assert child["youtube_category_id"] is None
        assert child["youtube_made_for_kids"] is None


# ---------------------------------------------------------------------------
# GET /accounts/ — pagination
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAccountsPagination:
    def test_accounts_pagination(self, multi_client, youtube_account, linkedin_account, zernio_youtube_account):
        r = multi_client.get("/api/v1/accounts/?limit=2")
        assert r.status_code == 200, r.content
        body = r.json()
        assert body["limit"] == 2
        assert len(body["accounts"]) == 2
        assert body["next_cursor"]
        r2 = multi_client.get(f"/api/v1/accounts/?limit=2&cursor={body['next_cursor']}")
        assert r2.status_code == 200, r2.content
        body2 = r2.json()
        assert len(body2["accounts"]) == 1
        assert body2["next_cursor"] is None
        seen = {a["id"] for a in body["accounts"]} | {a["id"] for a in body2["accounts"]}
        assert seen == {str(youtube_account.id), str(linkedin_account.id), str(zernio_youtube_account.id)}

    def test_accounts_default_returns_all(self, multi_client):
        body = multi_client.get("/api/v1/accounts/").json()
        assert len(body["accounts"]) == 3
        assert body["limit"] == 50
        assert body["next_cursor"] is None

    def test_accounts_bad_cursor_is_422(self, multi_client):
        r = multi_client.get("/api/v1/accounts/?cursor=!!!")
        assert r.status_code == 422
        assert r.json()["error"] == "unprocessable_entity"


# ---------------------------------------------------------------------------
# Error envelope
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestErrorEnvelope:
    def test_unknown_post_is_404_envelope(self, yt_client):
        r = yt_client.get(f"/api/v1/posts/{uuid.uuid4()}")
        assert r.status_code == 404
        body = r.json()
        assert body["error"] == "not_found"
        assert isinstance(body["detail"], str) and body["detail"]

    def test_validation_error_uses_envelope(self, yt_client):
        r = yt_client.post(
            "/api/v1/posts/",
            data=json.dumps({"social_account_id": "not-a-uuid", "caption": "x"}),
            content_type="application/json",
        )
        assert r.status_code == 422, r.content
        body = r.json()
        assert body["error"] == "unprocessable_entity"
        assert isinstance(body["detail"], str) and "social_account_id" in body["detail"]

    def test_missing_bearer_is_401_envelope(self):
        c = _SecureClient()
        r = c.get("/api/v1/me/")
        assert r.status_code == 401
        assert r.json()["error"] == "unauthorized"

    def test_live_bad_cursor_uses_envelope(self, yt_client):
        r = yt_client.get("/api/v1/live/?cursor=!!!")
        assert r.status_code == 422
        assert r.json()["error"] == "unprocessable_entity"


# ---------------------------------------------------------------------------
# Idempotency — body field wins over the header
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestIdempotencyBodyWins:
    def test_body_key_wins_over_header(self, yt_client, youtube_account):
        base = {"social_account_id": str(youtube_account.id), "caption": "Idempotent.", "action": "draft"}
        # Request 1: body key "body-k" + header "header-k" → stored under "body-k".
        r1 = yt_client.post(
            "/api/v1/posts/",
            data=json.dumps({**base, "idempotency_key": "body-k"}),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="header-k",
        )
        assert r1.status_code == 201, r1.content
        first_id = r1.json()["id"]
        # Request 2: header-only retry with the same header + same body.
        # If the header had won request 1, this would 422 (same key, the
        # stored fingerprint carries idempotency_key="body-k", not null).
        # Body-wins means the header key was never claimed → a fresh create.
        r2 = yt_client.post(
            "/api/v1/posts/",
            data=json.dumps(base),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="header-k",
        )
        assert r2.status_code == 201, r2.content
        assert r2.json()["id"] != first_id
        # Request 3: same body key + same body → replay of request 1.
        r3 = yt_client.post(
            "/api/v1/posts/",
            data=json.dumps({**base, "idempotency_key": "body-k"}),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="some-other-header",
        )
        assert r3.status_code == 201, r3.content
        assert r3.json()["id"] == first_id
