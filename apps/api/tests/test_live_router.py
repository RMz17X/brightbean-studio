"""Tests for the Live Agent API router (``/api/v1/live/*``).

No YouTube call is ever made: the provider is faked at the
``live_service._get_provider_for_platform`` seam and ``live_ready`` is
patched (tokeninfo would need a real Google grant).
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from django.test import Client
from django.utils import timezone

from apps.api_keys import services
from apps.members.models import PERMISSION_KEYS, OrgMembership, WorkspaceMembership
from apps.social_accounts.models import LiveBroadcast, SocialAccount

LIVE_KEY = "aaaa-bbbb-cccc-dddd"
NEW_KEY = "1111-2222-3333-4444"


class FakeLiveProvider:
    """Stand-in for YouTubeProvider's Live methods (canned, no network)."""

    def __init__(self):
        self.calls = []
        self.include_analytics_scopes = False

    def create_live_stream(self, token, *, title, resolution="1080p", frame_rate="30fps"):
        self.calls.append(("create_live_stream", title, resolution, frame_rate))
        return {
            "stream_id": "stream-test-1",
            "stream_name": LIVE_KEY,
            "ingestion_address": "rtmp://a.rtmp.youtube.com/live2",
            "stream_status": "active",
            "health_status": "good",
        }

    def create_live_broadcast(self, token, *, title, description="", privacy_status="private",
                              scheduled_start_time=None):
        self.calls.append(("create_live_broadcast", title, privacy_status))
        return {"broadcast_id": "bcast-test-1", "status": "ready"}

    def bind_live_broadcast(self, token, *, broadcast_id, stream_id):
        self.calls.append(("bind", broadcast_id, stream_id))
        return {"broadcast_id": broadcast_id, "status": "ready"}

    def transition_live_broadcast(self, token, *, broadcast_id, broadcast_status):
        self.calls.append(("transition", broadcast_id, broadcast_status))
        result = {"broadcast_id": broadcast_id, "status": broadcast_status}
        if broadcast_status == "live":
            result["actual_start_time"] = "2026-11-12T12:00:00Z"
        if broadcast_status == "complete":
            result["actual_end_time"] = "2026-11-12T13:00:00Z"
        return result

    def get_live_broadcast(self, token, *, broadcast_id):
        return {
            "broadcast_id": broadcast_id,
            "status": "live",
            "actual_start_time": "2026-11-12T12:00:00Z",
        }

    def get_live_stream(self, token, *, stream_id):
        return {"stream_status": "active", "health_status": "good"}

    def get_live_video_details(self, token, *, video_id):
        return {"concurrent_viewers": 42, "actual_start_time": "2026-11-12T12:00:00Z"}

    def delete_live_broadcast(self, token, *, broadcast_id):
        self.calls.append(("delete_broadcast", broadcast_id))
        return True

    def delete_live_stream(self, token, *, stream_id):
        self.calls.append(("delete_stream", stream_id))
        return True

    def get_granted_scopes(self, token):
        return ["https://www.googleapis.com/auth/youtube"]


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
        email="live-api-owner@example.com",
        password="secret-123",
        name="Live API Owner",
        tos_accepted_at=timezone.now(),
    )


@pytest.fixture
def organization(db):
    from apps.organizations.models import Organization

    return Organization.objects.create(name="Live API Org")


@pytest.fixture
def workspace(db, organization):
    from apps.workspaces.models import Workspace

    return Workspace.objects.create(name="Live API WS", organization=organization)


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
        account_platform_id="yt-api-1",
        account_name="YT Channel",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )


@pytest.fixture
def foreign_account(db, organization):
    """Account in a *different* workspace inside the same org."""
    from apps.workspaces.models import Workspace

    other = Workspace.objects.create(name="Other Live WS", organization=organization)
    return SocialAccount.objects.create(
        workspace=other,
        platform="youtube",
        account_platform_id="yt-other",
        account_name="Other YT",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )


@pytest.fixture
def issued_key(db, user, owner_memberships, workspace, youtube_account):
    return services.issue_api_key(
        workspace=workspace,
        social_accounts=[youtube_account],
        issued_by=user,
        name="live-smoke",
        permissions=list(PERMISSION_KEYS),
    )


@pytest.fixture
def readonly_key(db, user, owner_memberships, workspace, youtube_account):
    """Key without create_posts: reads work, writes 403."""
    perms = [p for p in PERMISSION_KEYS if p != "create_posts"]
    return services.issue_api_key(
        workspace=workspace,
        social_accounts=[youtube_account],
        issued_by=user,
        name="live-readonly",
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


def _broadcast(workspace, account, **kwargs):
    defaults = {
        "workspace": workspace,
        "social_account": account,
        "broadcast_id": "bcast-row-1",
        "stream_id": "stream-row-1",
        "title": "Row Live",
        "status": LiveBroadcast.Status.READY,
        "stream_url": "rtmp://a.rtmp.youtube.com/live2",
        "stream_key": LIVE_KEY,
    }
    defaults.update(kwargs)
    return LiveBroadcast.objects.create(**defaults)


def _post(client, path, payload):
    return client.post(path, data=json.dumps(payload), content_type="application/json")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAuth:
    def test_missing_token_returns_401(self):
        assert _SecureClient().get("/api/v1/live/").status_code == 401

    def test_bad_token_returns_401(self):
        c = _SecureClient(HTTP_AUTHORIZATION="Bearer invalid-token")
        assert c.get("/api/v1/live/").status_code == 401


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreate:
    def test_create_returns_201_with_full_key_once(self, api_client, youtube_account, provider):
        r = _post(
            api_client,
            "/api/v1/live/",
            {"social_account_id": str(youtube_account.id), "title": "API Live"},
        )
        assert r.status_code == 201, r.content
        body = r.json()
        assert body["broadcast_id"] == "bcast-test-1"
        assert body["status"] == "ready"
        assert body["stream_key"] == LIVE_KEY
        assert body["masked_stream_key"] == "••••-••••-••••-dddd"
        assert body["rtmps_url"] == "rtmps://a.rtmp.youtube.com:443/live2"
        assert body["watch_url"] == "https://www.youtube.com/watch?v=bcast-test-1"
        assert body["key_shown"] is True
        row = LiveBroadcast.objects.get(broadcast_id="bcast-test-1")
        assert row.key_shown is True
        assert ("create_live_stream", "API Live", "1080p", "30fps") in provider.calls
        assert ("bind", "bcast-test-1", "stream-test-1") in provider.calls

    def test_create_with_explicit_options(self, api_client, youtube_account, provider):
        r = _post(
            api_client,
            "/api/v1/live/",
            {
                "social_account_id": str(youtube_account.id),
                "title": "Scheduled",
                "description": "desc",
                "privacy_status": "unlisted",
                "resolution": "720p",
                "frame_rate": "60fps",
                "scheduled_start": "2026-11-13T10:00:00Z",
            },
        )
        assert r.status_code == 201, r.content
        assert r.json()["privacy_status"] == "unlisted"
        assert ("create_live_stream", "Scheduled", "720p", "60fps") in provider.calls

    def test_create_rejects_foreign_account(self, api_client, foreign_account, provider):
        r = _post(
            api_client,
            "/api/v1/live/",
            {"social_account_id": str(foreign_account.id), "title": "Nope"},
        )
        assert r.status_code == 403
        assert LiveBroadcast.objects.count() == 0

    def test_create_requires_create_posts_permission(self, readonly_client, youtube_account):
        r = _post(
            readonly_client,
            "/api/v1/live/",
            {"social_account_id": str(youtube_account.id), "title": "Nope"},
        )
        assert r.status_code == 403

    def test_create_validates_title(self, api_client, youtube_account):
        r = _post(api_client, "/api/v1/live/", {"social_account_id": str(youtube_account.id), "title": ""})
        assert r.status_code == 422

    def test_create_scope_missing_returns_403(self, api_client, youtube_account, provider, monkeypatch):
        monkeypatch.setattr("apps.social_accounts.live_service.live_ready", lambda account: False)
        r = _post(
            api_client,
            "/api/v1/live/",
            {"social_account_id": str(youtube_account.id), "title": "Nope"},
        )
        assert r.status_code == 403
        assert "Live permission" in r.json()["detail"]
        assert LiveBroadcast.objects.count() == 0


# ---------------------------------------------------------------------------
# List / detail — key is never disclosed again
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRead:
    def test_list_returns_masked_key_only(self, api_client, workspace, youtube_account):
        _broadcast(workspace, youtube_account)
        _broadcast(
            workspace,
            youtube_account,
            broadcast_id="bcast-row-2",
            title="Second",
            status=LiveBroadcast.Status.LIVE,
        )
        r = api_client.get("/api/v1/live/")
        assert r.status_code == 200, r.content
        body = r.json()
        assert len(body["broadcasts"]) == 2
        assert body["limit"] == 50
        assert body["next_cursor"] is None
        for item in body["broadcasts"]:
            assert item["stream_key"] is None
            assert item["masked_stream_key"] == "••••-••••-••••-dddd"
            assert LIVE_KEY not in json.dumps(body)

    def test_list_status_filter_and_pagination(self, api_client, workspace, youtube_account):
        _broadcast(workspace, youtube_account)
        _broadcast(
            workspace,
            youtube_account,
            broadcast_id="bcast-row-2",
            title="Second",
            status=LiveBroadcast.Status.LIVE,
        )
        r = api_client.get("/api/v1/live/?status=live")
        assert [b["broadcast_id"] for b in r.json()["broadcasts"]] == ["bcast-row-2"]
        r = api_client.get("/api/v1/live/?limit=1")
        body = r.json()
        assert len(body["broadcasts"]) == 1
        assert body["next_cursor"]
        r2 = api_client.get(f"/api/v1/live/?limit=1&cursor={body['next_cursor']}")
        assert len(r2.json()["broadcasts"]) == 1

    def test_list_bad_status_and_cursor_are_422(self, api_client):
        assert api_client.get("/api/v1/live/?status=nope").status_code == 422
        assert api_client.get("/api/v1/live/?cursor=!!!").status_code == 422

    def test_detail_refreshes_active_broadcast(self, api_client, workspace, youtube_account, provider):
        row = _broadcast(workspace, youtube_account, status=LiveBroadcast.Status.LIVE)
        r = api_client.get(f"/api/v1/live/{row.id}")
        assert r.status_code == 200, r.content
        body = r.json()
        assert body["viewers"] == 42
        assert body["stream_status"] == "active"
        assert body["health_status"] == "good"
        assert body["stream_key"] is None

    def test_detail_foreign_broadcast_is_404(self, api_client, workspace, youtube_account, foreign_account):
        row = LiveBroadcast.objects.create(
            workspace=foreign_account.workspace,
            social_account=foreign_account,
            broadcast_id="bcast-foreign",
            stream_id="stream-foreign",
            title="Foreign",
            status=LiveBroadcast.Status.READY,
            stream_url="rtmp://a.rtmp.youtube.com/live2",
            stream_key=LIVE_KEY,
        )
        assert api_client.get(f"/api/v1/live/{row.id}").status_code == 404


# ---------------------------------------------------------------------------
# Transition — public go-live needs confirm
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTransition:
    def test_go_live_public_without_confirm_is_400(self, api_client, workspace, youtube_account):
        row = _broadcast(workspace, youtube_account, privacy_status="public")
        r = _post(api_client, f"/api/v1/live/{row.id}/transition", {"action": "go_live"})
        assert r.status_code == 400
        assert "confirm" in r.json()["detail"]
        row.refresh_from_db()
        assert row.status == LiveBroadcast.Status.READY

    def test_go_live_public_with_confirm(self, api_client, workspace, youtube_account, provider):
        row = _broadcast(workspace, youtube_account, privacy_status="public")
        r = _post(
            api_client,
            f"/api/v1/live/{row.id}/transition",
            {"action": "go_live", "confirm": True},
        )
        assert r.status_code == 200, r.content
        assert r.json()["status"] == "live"
        assert ("transition", "bcast-row-1", "live") in provider.calls

    def test_go_live_private_needs_no_confirm(self, api_client, workspace, youtube_account, provider):
        row = _broadcast(workspace, youtube_account, privacy_status="private")
        r = _post(api_client, f"/api/v1/live/{row.id}/transition", {"action": "go_live"})
        assert r.status_code == 200, r.content
        assert r.json()["status"] == "live"

    def test_test_and_stop(self, api_client, workspace, youtube_account, provider):
        row = _broadcast(workspace, youtube_account)
        r = _post(api_client, f"/api/v1/live/{row.id}/transition", {"action": "test"})
        assert r.status_code == 200 and r.json()["status"] == "testing"
        r = _post(api_client, f"/api/v1/live/{row.id}/transition", {"action": "stop"})
        assert r.status_code == 200 and r.json()["status"] == "complete"

    def test_transition_unknown_action_is_422(self, api_client, workspace, youtube_account):
        row = _broadcast(workspace, youtube_account)
        r = _post(api_client, f"/api/v1/live/{row.id}/transition", {"action": "explode"})
        assert r.status_code == 422

    def test_transition_requires_create_posts(self, readonly_client, workspace, youtube_account):
        row = _broadcast(workspace, youtube_account)
        r = _post(readonly_client, f"/api/v1/live/{row.id}/transition", {"action": "test"})
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# Recreate stream / delete
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRecreateDelete:
    def test_recreate_stream_returns_new_key_once(self, api_client, workspace, youtube_account, provider):
        row = _broadcast(workspace, youtube_account)
        provider.create_live_stream = lambda token, **kw: {
            "stream_id": "stream-test-2",
            "stream_name": NEW_KEY,
            "ingestion_address": "rtmp://a.rtmp.youtube.com/live2",
            "stream_status": "active",
            "health_status": "good",
        }
        r = _post(api_client, f"/api/v1/live/{row.id}/recreate-stream", {})
        assert r.status_code == 200, r.content
        body = r.json()
        assert body["stream_key"] == NEW_KEY
        assert body["stream_id"] == "stream-test-2"
        assert body["key_shown"] is True
        row.refresh_from_db()
        assert row.stream_key == NEW_KEY

    def test_recreate_stream_refused_once_live(self, api_client, workspace, youtube_account, provider):
        row = _broadcast(workspace, youtube_account, status=LiveBroadcast.Status.LIVE)
        r = _post(api_client, f"/api/v1/live/{row.id}/recreate-stream", {})
        assert r.status_code == 422

    def test_delete_removes_row_and_calls_youtube(self, api_client, workspace, youtube_account, provider):
        row = _broadcast(workspace, youtube_account)
        r = api_client.delete(f"/api/v1/live/{row.id}")
        assert r.status_code == 200, r.content
        assert r.json() == {"id": str(row.id), "broadcast_id": "bcast-row-1", "deleted": True}
        assert not LiveBroadcast.objects.filter(pk=row.id).exists()
        assert ("delete_broadcast", "bcast-row-1") in provider.calls
        assert ("delete_stream", "stream-row-1") in provider.calls


# ---------------------------------------------------------------------------
# Create idempotency — same key replays, never a second YouTube broadcast
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateIdempotency:
    def _create_body(self, youtube_account, key=None, title="Idempotent Live"):
        body = {"social_account_id": str(youtube_account.id), "title": title}
        if key is not None:
            body["idempotency_key"] = key
        return body

    def _yt_create_calls(self, provider):
        return [c for c in provider.calls if c[0] in ("create_live_stream", "create_live_broadcast", "bind")]

    def test_same_key_same_body_replays_without_second_broadcast(
        self, api_client, youtube_account, provider
    ):
        body = self._create_body(youtube_account, key="live-once-1")
        r1 = _post(api_client, "/api/v1/live/", body)
        assert r1.status_code == 201, r1.content
        r2 = _post(api_client, "/api/v1/live/", body)
        assert r2.status_code == 201, r2.content
        # Replay is byte-identical: same logical create, same stream key.
        assert r1.json() == r2.json()
        assert LiveBroadcast.objects.count() == 1
        # YouTube saw exactly one stream + one broadcast + one bind.
        assert len(self._yt_create_calls(provider)) == 3
        assert provider.calls.count(("create_live_stream", "Idempotent Live", "1080p", "30fps")) == 1
        assert provider.calls.count(("create_live_broadcast", "Idempotent Live", "private")) == 1

    def test_same_key_via_header_replays(self, api_client, youtube_account, provider):
        body = self._create_body(youtube_account)
        kwargs = {"HTTP_IDEMPOTENCY_KEY": "live-header-1"}
        r1 = api_client.post(
            "/api/v1/live/", data=json.dumps(body), content_type="application/json", **kwargs
        )
        assert r1.status_code == 201, r1.content
        r2 = api_client.post(
            "/api/v1/live/", data=json.dumps(body), content_type="application/json", **kwargs
        )
        assert r2.status_code == 201, r2.content
        assert r1.json() == r2.json()
        assert LiveBroadcast.objects.count() == 1
        assert len(self._yt_create_calls(provider)) == 3

    def test_different_key_creates_second_broadcast(self, api_client, youtube_account, provider):
        # The canned fake returns one fixed broadcast_id, which is
        # unique in the DB — give each create a distinct YouTube id so
        # the second create can actually persist.
        counter = {"n": 0}

        def _bcast(token, **kw):
            counter["n"] += 1
            return {"broadcast_id": f"bcast-test-{counter['n']}", "status": "ready"}

        provider.create_live_broadcast = _bcast
        r1 = _post(api_client, "/api/v1/live/", self._create_body(youtube_account, key="live-key-a"))
        r2 = _post(api_client, "/api/v1/live/", self._create_body(youtube_account, key="live-key-b"))
        assert r1.status_code == 201, r1.content
        assert r2.status_code == 201, r2.content
        assert r1.json()["id"] != r2.json()["id"]
        assert r1.json()["broadcast_id"] != r2.json()["broadcast_id"]
        assert LiveBroadcast.objects.count() == 2

    def test_same_key_different_body_is_422(self, api_client, youtube_account, provider):
        r1 = _post(api_client, "/api/v1/live/", self._create_body(youtube_account, key="live-reuse-1"))
        assert r1.status_code == 201, r1.content
        r2 = _post(
            api_client,
            "/api/v1/live/",
            self._create_body(youtube_account, key="live-reuse-1", title="Different title"),
        )
        assert r2.status_code == 422, r2.content
        assert LiveBroadcast.objects.count() == 1
        assert len(self._yt_create_calls(provider)) == 3

    def test_in_flight_claim_returns_409_and_creates_nothing(
        self, api_client, youtube_account, provider, issued_key
    ):
        """A peer holding the slot (PENDING) must 409, not call YouTube."""
        from apps.api.middleware import PENDING_STATUS_SENTINEL, fingerprint_request
        from apps.api.models import IdempotencyRecord
        from apps.api.schemas import LiveBroadcastCreateRequest

        body = self._create_body(youtube_account, key="live-race-1")
        # Fingerprint exactly as the route computes it: over the
        # Pydantic schema's canonical dict (defaults filled in).
        canonical = LiveBroadcastCreateRequest(**body).model_dump(mode="json")
        fp = fingerprint_request("POST", "/api/v1/live/", canonical)
        IdempotencyRecord.objects.create(
            api_key=issued_key.api_key,
            key="live-race-1",
            request_fingerprint=fp,
            response_status=PENDING_STATUS_SENTINEL,
            response_body={},
        )
        r = _post(api_client, "/api/v1/live/", body)
        assert r.status_code == 409, r.content
        assert LiveBroadcast.objects.count() == 0
        assert self._yt_create_calls(provider) == []

    def test_failed_create_releases_claim_so_retry_can_succeed(
        self, api_client, youtube_account, provider, monkeypatch
    ):
        """Scope failure happens after the claim; the slot must be freed
        so the same key can retry once the account is reconnected."""
        from apps.api.models import IdempotencyRecord

        body = self._create_body(youtube_account, key="live-retry-1")
        monkeypatch.setattr("apps.social_accounts.live_service.live_ready", lambda account: False)
        r1 = _post(api_client, "/api/v1/live/", body)
        assert r1.status_code == 403, r1.content
        assert LiveBroadcast.objects.count() == 0
        assert self._yt_create_calls(provider) == []
        assert not IdempotencyRecord.objects.filter(key="live-retry-1").exists()
        monkeypatch.setattr("apps.social_accounts.live_service.live_ready", lambda account: True)
        r2 = _post(api_client, "/api/v1/live/", body)
        assert r2.status_code == 201, r2.content
        assert LiveBroadcast.objects.count() == 1
        assert len(self._yt_create_calls(provider)) == 3
