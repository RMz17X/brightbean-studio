"""Tests for the two Live dashboard UX refinements (2026-11-12):
1. Optional create-form fields (resolution, frame rate, scheduled start)
   live inside a closed-by-default <details> "Advanced" block.
2. Go Live asks for confirmation only when the broadcast is public.
"""
from unittest.mock import patch

from django.urls import reverse

from apps.social_accounts.tests.test_live_views import LiveViewsTestsBase


class LiveUxTests(LiveViewsTestsBase):
    @patch("apps.social_accounts.live_views._live_ready", return_value=True)
    def test_create_form_folds_optional_fields_into_advanced(self, _mock_ready):
        self._youtube_account()
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.status_code, 200)
        # Always-visible fields.
        self.assertContains(response, 'name="account_id"')
        self.assertContains(response, 'name="title"')
        self.assertContains(response, 'name="privacy_status"')
        # Optional fields exist but sit inside the closed Advanced block.
        self.assertContains(response, "<details")
        self.assertContains(response, "Advanced")
        self.assertContains(response, 'name="resolution"')
        self.assertContains(response, 'name="frame_rate"')
        self.assertContains(response, 'name="scheduled_start"')
        # Defaults unchanged: 1080p / 30fps pre-selected.
        self.assertContains(response, '<option value="1080p" selected>')
        self.assertContains(response, '<option value="30fps" selected>')
        self.assertContains(response, '<option value="private" selected>')

    @patch("apps.social_accounts.live_views._live_ready", return_value=True)
    @patch("apps.social_accounts.live_views._refresh_broadcast_status", return_value=False)
    def test_go_live_confirms_for_public_broadcast(self, _mock_refresh, _mock_ready):
        # The Go Live button renders on the monitor card for TESTING
        # (and, since the READY-direct change, for READY too — see the
        # tests below); this test keeps the testing -> live case.
        from apps.social_accounts.models import LiveBroadcast

        account = self._youtube_account()
        self._broadcast(account, privacy_status="public", status=LiveBroadcast.Status.TESTING)
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Go Live")
        self.assertContains(response, "Go live now? This broadcast is PUBLIC")

    @patch("apps.social_accounts.live_views._live_ready", return_value=True)
    @patch("apps.social_accounts.live_views._refresh_broadcast_status", return_value=False)
    def test_go_live_has_no_confirm_for_private_broadcast(self, _mock_refresh, _mock_ready):
        from apps.social_accounts.models import LiveBroadcast

        account = self._youtube_account()
        self._broadcast(account, privacy_status="private", status=LiveBroadcast.Status.TESTING)
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Go Live")
        self.assertNotContains(response, "Go live now?")

    # -- READY can Go Live straight from the monitor (no Start test first) --
    @patch("apps.social_accounts.live_views._live_ready", return_value=True)
    def test_ready_public_broadcast_shows_go_live_with_confirm(self, _mock_ready):
        from apps.social_accounts.models import LiveBroadcast

        account = self._youtube_account()
        self._broadcast(account, privacy_status="public", status=LiveBroadcast.Status.READY)
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ready, testing, or on air")
        self.assertContains(response, "Go Live")
        self.assertContains(response, 'value="go_live"')
        self.assertContains(response, "Go live now? This broadcast is PUBLIC")
        # Start test stays available as an option, it is just not required.
        self.assertContains(response, "Start test")

    @patch("apps.social_accounts.live_views._live_ready", return_value=True)
    def test_ready_private_broadcast_go_live_has_no_confirm(self, _mock_ready):
        from apps.social_accounts.models import LiveBroadcast

        account = self._youtube_account()
        self._broadcast(account, privacy_status="private", status=LiveBroadcast.Status.READY)
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Go Live")
        self.assertContains(response, 'value="go_live"')
        self.assertNotContains(response, "Go live now?")

    @patch("apps.social_accounts.live_views._live_ready", return_value=True)
    def test_complete_broadcast_has_no_go_live_button(self, _mock_ready):
        from apps.social_accounts.models import LiveBroadcast

        account = self._youtube_account()
        self._broadcast(account, status=LiveBroadcast.Status.COMPLETE)
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'value="go_live"')
        self.assertNotContains(response, "Ready, testing, or on air")

    def test_is_actionable_covers_ready_but_is_active_does_not(self):
        from apps.social_accounts.models import LiveBroadcast

        account = self._youtube_account()
        broadcast = self._broadcast(account, status=LiveBroadcast.Status.READY)
        self.assertFalse(broadcast.is_active)
        self.assertTrue(broadcast.is_actionable)
        broadcast.status = LiveBroadcast.Status.TESTING
        self.assertTrue(broadcast.is_active)
        self.assertTrue(broadcast.is_actionable)
        broadcast.status = LiveBroadcast.Status.LIVE
        self.assertTrue(broadcast.is_active)
        self.assertTrue(broadcast.is_actionable)
        broadcast.status = LiveBroadcast.Status.COMPLETE
        self.assertFalse(broadcast.is_active)
        self.assertFalse(broadcast.is_actionable)

    @patch("apps.social_accounts.live_service.transition_live_broadcast")
    def test_ready_broadcast_go_live_transition_posts_to_live(self, mock_transition):
        from apps.social_accounts.models import LiveBroadcast

        account = self._youtube_account()
        broadcast = self._broadcast(account, status=LiveBroadcast.Status.READY)
        url = reverse(
            "live:transition",
            kwargs={"workspace_id": self.workspace.pk, "broadcast_id": broadcast.pk},
        )
        response = self.client.post(url, {"action": "go_live"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], self.dashboard_url)
        mock_transition.assert_called_once()
        called_broadcast, called_target = mock_transition.call_args.args
        self.assertEqual(called_broadcast.pk, broadcast.pk)
        self.assertEqual(called_target, "live")
