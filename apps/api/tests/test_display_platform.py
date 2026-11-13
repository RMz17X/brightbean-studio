"""API responses expose the destination platform for Zernio-routed accounts.

A consumer of ``/me``, ``/accounts`` or a post detail must see TikTok /
YouTube, never the transport name ``"zernio"``; the transport is still
visible in the additive ``transport`` field (``"zernio"`` or ``null``).
"""

from __future__ import annotations

import pytest
from django.test import Client
from django.utils import timezone

from apps.api_keys import services
from apps.composer.services import create_post
from apps.inbox.models import InboxMessage
from apps.api.schemas import InboxMessageResponse
from django.utils import timezone as dj_timezone
from apps.members.models import PERMISSION_KEYS, OrgMembership, WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.zernio.models import ZernioAccountBinding, ZernioApiKey

SK_KEY = "sk_" + "f" * 64


class _SecureClient(Client):
    def generic(self, method, path, *args, **kwargs):
        kwargs["secure"] = True
        return super().generic(method, path, *args, **kwargs)


@pytest.fixture
def user(db):
    from apps.accounts.models import User

    return User.objects.create_user(
        email="disp@example.com",
        password="test-password-123",
        name="Display",
        tos_accepted_at=timezone.now(),
    )


@pytest.fixture
def organization(db):
    from apps.organizations.models import Organization

    return Organization.objects.create(name="Display Org")


@pytest.fixture
def workspace(db, organization):
    from apps.workspaces.models import Workspace

    return Workspace.objects.create(name="Display WS", organization=organization)


@pytest.fixture
def owner_memberships(db, user, organization, workspace):
    OrgMembership.objects.create(user=user, organization=organization, org_role=OrgMembership.OrgRole.OWNER)
    return WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )


def _make_account(workspace, platform: str, **kwargs):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform=platform,
        account_platform_id=kwargs.pop("account_platform_id", f"{platform}-id"),
        account_name=kwargs.pop("account_name", f"{platform} acct"),
        connection_status=kwargs.pop("connection_status", "connected"),
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


def _issued_key(workspace, user, social_accounts):
    return services.issue_api_key(
        workspace=workspace,
        social_accounts=social_accounts,
        issued_by=user,
        name="display",
        permissions=list(PERMISSION_KEYS),
    )


@pytest.mark.django_db
class TestAccountSummariesShowDestination:
    def test_me_lists_zernio_tiktok_as_tiktok(self, user, owner_memberships, workspace):
        account = _make_account(workspace, "zernio", account_name="BeritaKenapa")
        _bind(workspace, account, "tiktok")
        key = _issued_key(workspace, user, [account])
        client = _SecureClient(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")
        body = client.get("/api/v1/me/").json()
        (row,) = body["allowlisted_accounts"]
        assert row["platform"] == "tiktok"
        assert row["transport"] == "zernio"
        assert row["char_limit"] == 2200
        assert row["requires_video"] is True
        assert row["needs_title"] is False
        assert row["supports_first_comment"] is False

    def test_me_keeps_direct_accounts_unchanged(self, user, owner_memberships, workspace):
        account = _make_account(workspace, "youtube")
        key = _issued_key(workspace, user, [account])
        client = _SecureClient(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")
        body = client.get("/api/v1/me/").json()
        (row,) = body["allowlisted_accounts"]
        assert row["platform"] == "youtube"
        assert row["transport"] is None
        assert row["char_limit"] == 5000
        assert row["needs_title"] is True

    def test_post_detail_platform_post_is_destination(self, user, owner_memberships, workspace):
        account = _make_account(workspace, "zernio", account_name="BeritaKenapa")
        _bind(workspace, account, "tiktok")
        post = create_post(
            workspace=workspace,
            social_account=account,
            caption="draft via zernio",
            media_asset_ids=[],
            scheduled_at=None,
            status="draft",
        )
        key = _issued_key(workspace, user, [account])
        client = _SecureClient(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")
        body = client.get(f"/api/v1/posts/{post.pk}").json()  # Ninja: no trailing slash on /{id}
        (child,) = body["platform_posts"]
        assert child["platform"] == "tiktok"
        assert child["transport"] == "zernio"


@pytest.mark.django_db
class TestInboxMessageShowsDestination:
    def test_zernio_message_response_names_destination(self, workspace):
        from apps.zernio.models import ZernioAccountBinding, ZernioApiKey

        account = _make_account(workspace, "zernio", account_name="BeritaKenapa")
        _bind(workspace, account, "tiktok")
        message = InboxMessage.objects.create(
            workspace=workspace,
            social_account=account,
            platform_message_id="msg-1",
            sender_name="Someone",
            body="hello",
            received_at=dj_timezone.now(),
        )
        assert message.platform == "tiktok"
        response = InboxMessageResponse.from_message(message)
        assert response.platform == "tiktok"
        assert response.transport == "zernio"
