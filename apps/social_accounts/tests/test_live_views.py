"""Tests for the YouTube Live monitor views.

Only routing, auth gating and rendering are exercised here — no YouTube
call is ever made. ``_live_ready`` is patched (tokeninfo would need a
real Google grant), and the dashboard's active-broadcast poll is skipped
because the fixture broadcasts are not active.
"""

from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import LiveBroadcast, SocialAccount
from apps.workspaces.models import Workspace


class LiveViewsTestsBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="live-owner@example.com",
            password="testpass123",
            tos_accepted_at=timezone.now(),
        )
        self.org = Organization.objects.create(name="Live Org")
        self.workspace = Workspace.objects.create(organization=self.org, name="Live WS")
        OrgMembership.objects.create(
            user=self.user,
            organization=self.org,
            org_role=OrgMembership.OrgRole.OWNER,
        )
        WorkspaceMembership.objects.create(
            user=self.user,
            workspace=self.workspace,
            workspace_role=WorkspaceMembership.WorkspaceRole.OWNER,
        )
        self.client.force_login(self.user)
        self.dashboard_url = reverse("live:dashboard", kwargs={"workspace_id": self.workspace.pk})

    def _youtube_account(self, **kwargs):
        defaults = {
            "workspace": self.workspace,
            "platform": "youtube",
            "account_platform_id": "yt-live-1",
            "account_name": "YT Channel",
            "connection_status": SocialAccount.ConnectionStatus.CONNECTED,
        }
        defaults.update(kwargs)
        return SocialAccount.objects.create(**defaults)

    def _broadcast(self, account, **kwargs):
        defaults = {
            "workspace": self.workspace,
            "social_account": account,
            "broadcast_id": "bcast-live-1",
            "stream_id": "stream-live-1",
            "title": "Test Live",
            "status": LiveBroadcast.Status.READY,
            "stream_url": "rtmp://a.rtmp.youtube.com/live2",
            "stream_key": "abcd-efgh-ijkl-mnop",
        }
        defaults.update(kwargs)
        return LiveBroadcast.objects.create(**defaults)


class LiveDashboardTests(LiveViewsTestsBase):
    def test_logged_out_redirects_to_login(self):
        self.client.logout()
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_non_member_gets_403(self):
        outsider = User.objects.create_user(
            email="outsider@example.com", password="testpass123", tos_accepted_at=timezone.now()
        )
        self.client.force_login(outsider)
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.status_code, 403)

    @patch("apps.social_accounts.live_views._live_ready", return_value=True)
    def test_dashboard_renders_for_member_with_no_accounts(self, _mock_ready):
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "YouTube Live")
        self.assertContains(response, "No YouTube account connected")

    @patch("apps.social_accounts.live_views._live_ready", return_value=False)
    def test_dashboard_shows_reauthorize_banner_when_scope_missing(self, _mock_ready):
        self._youtube_account()
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Live permission missing")
        self.assertContains(response, "Reconnect for Live")

    @patch("apps.social_accounts.live_views._live_ready", return_value=True)
    @patch("apps.social_accounts.live_views._refresh_broadcast_status", return_value=False)
    def test_dashboard_lists_active_broadcast_with_masked_key(self, _mock_refresh, _mock_ready):
        account = self._youtube_account()
        self._broadcast(account, status=LiveBroadcast.Status.TESTING)
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test Live")
        # Masked key is shown; the full key must never appear in page HTML.
        self.assertContains(response, "••••-••••-••••-mnop")
        self.assertNotContains(response, "abcd-efgh-ijkl-mnop")
        self.assertContains(response, "rtmps://a.rtmp.youtube.com:443/live2")

    @patch("apps.social_accounts.live_views._live_ready", return_value=True)
    def test_status_endpoint_empty_when_nothing_active(self, _mock_ready):
        account = self._youtube_account()
        self._broadcast(account)
        response = self.client.get(reverse("live:status", kwargs={"workspace_id": self.workspace.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"broadcasts": [], "poll_interval_seconds": 60})


class LiveCreatedTests(LiveViewsTestsBase):
    @patch("apps.social_accounts.live_views._live_ready", return_value=True)
    def test_created_page_shows_key_once_then_masked(self, _mock_ready):
        account = self._youtube_account()
        broadcast = self._broadcast(account)
        url = reverse(
            "live:created",
            kwargs={"workspace_id": self.workspace.pk, "broadcast_id": broadcast.pk},
        )
        first = self.client.get(url)
        self.assertEqual(first.status_code, 200)
        self.assertContains(first, "abcd-efgh-ijkl-mnop")
        broadcast.refresh_from_db()
        self.assertTrue(broadcast.key_shown)
        second = self.client.get(url)
        self.assertEqual(second.status_code, 200)
        self.assertNotContains(second, "abcd-efgh-ijkl-mnop")
        self.assertContains(second, "••••-••••-••••-mnop")


class LiveModelTests(LiveViewsTestsBase):
    def test_watch_url_and_masked_key_properties(self):
        account = self._youtube_account()
        broadcast = self._broadcast(account)
        self.assertEqual(broadcast.watch_url, "https://www.youtube.com/watch?v=bcast-live-1")
        self.assertEqual(broadcast.masked_stream_key, "••••-••••-••••-mnop")
        self.assertFalse(broadcast.is_active)
        broadcast.status = LiveBroadcast.Status.LIVE
        self.assertTrue(broadcast.is_active)
