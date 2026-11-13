"""Daily-post quota buckets follow the destination platform, not the transport.

Zernio is a transport, not a platform: a Zernio-bound account posts to its
binding's destination, so it must draw from that destination's bucket in
``PLATFORM_DAILY_POST_LIMIT`` (``apps/api/limits.py``) — Zernio-TikTok gets
TikTok's 15/day, Zernio-YouTube gets YouTube's 50/day — instead of falling
through to the 50/day default for every Zernio account. Direct accounts are
unaffected, and an unbound Zernio account keeps the documented 50/day
fallback (``"zernio"`` has no key in the map, by design).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from ninja.errors import HttpError

from apps.api.limits import (
    PLATFORM_DAILY_POST_LIMIT,
    check_platform_quota,
    resolve_platform_limit,
)
from apps.composer.models import PlatformPost, Post
from apps.social_accounts.models import SocialAccount
from apps.zernio.models import ZernioAccountBinding, ZernioApiKey

SK_KEY = "sk_" + "q" * 64


@pytest.fixture
def workspace(db, organization):
    from apps.workspaces.models import Workspace

    return Workspace.objects.create(name="Quota WS", organization=organization)


def _account(workspace, platform, **kwargs):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform=platform,
        account_platform_id=kwargs.pop("account_platform_id", f"{platform}-quota-1"),
        account_name=kwargs.pop("account_name", f"{platform} quota acct"),
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


def _fill_bucket(account, n):
    """Create ``n`` quota-consuming (scheduled) rows for ``account``."""
    for _ in range(n):
        PlatformPost.objects.create(
            post=Post.objects.create(workspace=account.workspace, caption="x"),
            social_account=account,
            status="scheduled",
            scheduled_at=timezone.now() + timedelta(hours=1),
        )


@pytest.mark.django_db
class TestResolvePlatformLimitIsDestinationAware:
    @pytest.mark.parametrize(
        "destination,expected",
        [
            ("tiktok", 15),
            ("youtube", 50),
            ("facebook", 200),
            ("instagram", 25),
            ("threads", 250),
            ("pinterest", 100),
            ("bluesky", 200),
            ("google_business", 50),
        ],
    )
    def test_zernio_account_gets_destination_limit(self, workspace, destination, expected):
        # Expected values are pinned literally here (not read back out of
        # the map) so the test fails if the map's numbers ever drift.
        assert PLATFORM_DAILY_POST_LIMIT[destination] == expected
        acc = _account(workspace, "zernio", account_name="BeritaKenapa")
        _bind(workspace, acc, destination)
        acc = SocialAccount.objects.get(pk=acc.pk)
        assert acc.display_platform == destination
        assert resolve_platform_limit(acc) == expected

    @pytest.mark.parametrize(
        "platform,expected",
        [
            ("tiktok", 15),
            ("youtube", 50),
            ("facebook", 200),
            ("instagram", 25),
            ("threads", 250),
            ("linkedin_personal", 100),
            ("linkedin_company", 100),
        ],
    )
    def test_direct_account_limit_unchanged(self, workspace, platform, expected):
        acc = _account(workspace, platform)
        assert acc.display_platform == platform
        assert resolve_platform_limit(acc) == expected

    def test_zernio_unbound_falls_back_to_default(self, workspace):
        acc = _account(workspace, "zernio")
        assert acc.display_platform == "zernio"
        assert "zernio" not in PLATFORM_DAILY_POST_LIMIT
        assert resolve_platform_limit(acc) == 50

    def test_zernio_linkedin_destination_has_no_map_key_so_falls_back(self, workspace):
        # "linkedin" (the Zernio destination slug) is not a map key — the
        # map only carries "linkedin_personal" / "linkedin_company" — so
        # it lands on the documented 50/day fallback rather than inventing
        # a LinkedIn cap.
        acc = _account(workspace, "zernio")
        _bind(workspace, acc, "linkedin")
        acc = SocialAccount.objects.get(pk=acc.pk)
        assert acc.display_platform == "linkedin"
        assert resolve_platform_limit(acc) == 50

    def test_per_account_override_still_wins_for_zernio_account(self, workspace):
        acc = _account(workspace, "zernio", daily_post_limit_override=3)
        _bind(workspace, acc, "tiktok")
        acc = SocialAccount.objects.get(pk=acc.pk)
        assert resolve_platform_limit(acc) == 3

    def test_unsaved_zernio_account_does_not_crash_and_falls_back(self):
        acc = SocialAccount(platform="zernio")
        assert acc.display_platform == "zernio"
        assert resolve_platform_limit(acc) == 50


@pytest.mark.django_db
class TestCheckPlatformQuotaUsesDestinationBucket:
    def test_zernio_tiktok_429s_at_15_not_50(self, workspace):
        acc = _account(workspace, "zernio", account_name="BeritaKenapa")
        _bind(workspace, acc, "tiktok")
        acc = SocialAccount.objects.get(pk=acc.pk)
        _fill_bucket(acc, 15)
        with pytest.raises(HttpError) as excinfo:
            check_platform_quota(acc)
        assert excinfo.value.status_code == 429
        assert "platform_quota:tiktok" in excinfo.value.message
        assert "limit=15" in excinfo.value.message

    def test_zernio_youtube_still_has_room_at_15(self, workspace):
        acc = _account(workspace, "zernio", account_name="BeritaKenapa")
        _bind(workspace, acc, "youtube")
        acc = SocialAccount.objects.get(pk=acc.pk)
        _fill_bucket(acc, 15)
        # YouTube's bucket is 50/day, so 15 used must NOT trip the quota.
        check_platform_quota(acc)

    def test_direct_tiktok_429s_at_15(self, workspace):
        acc = _account(workspace, "tiktok")
        _fill_bucket(acc, 15)
        with pytest.raises(HttpError) as excinfo:
            check_platform_quota(acc)
        assert excinfo.value.status_code == 429
        assert "platform_quota:tiktok" in excinfo.value.message
        assert "limit=15" in excinfo.value.message
