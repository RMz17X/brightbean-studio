"""Tests for the Zernio key-pool views.

Only routing, auth gating and rendering are exercised — Zernio itself is
patched out (``services.test_key``), so no network call is ever made.
"""

from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace
from providers.exceptions import ProviderError

from ..models import ZernioAccountBinding, ZernioApiKey

SK_KEY = "sk_" + "b" * 64

DISCOVERED = [
    {
        "account_id": "acc-yt",
        "platform": "youtube",
        "username": "beritakenapa",
        "display_name": "BeritaKenapa",
        "is_active": True,
        "profile_id": "prof1",
    },
    {
        "account_id": "acc-tt",
        "platform": "tiktok",
        "username": "beritakenapa",
        "display_name": "BeritaKenapa",
        "is_active": True,
        "profile_id": "prof1",
    },
]


class ZernioViewsTestsBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="zernio-owner@example.com",
            password="x",
            tos_accepted_at=timezone.now(),
        )
        self.org = Organization.objects.create(name="Zernio Org")
        self.workspace = Workspace.objects.create(organization=self.org, name="Zernio WS")
        OrgMembership.objects.create(
            user=self.user, organization=self.org, org_role=OrgMembership.OrgRole.OWNER
        )
        WorkspaceMembership.objects.create(
            user=self.user, workspace=self.workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
        )
        self.client.force_login(self.user)
        self.dashboard_url = reverse("zernio:dashboard", kwargs={"workspace_id": self.workspace.pk})

    def _key(self, label="Key 1"):
        return ZernioApiKey.objects.create(workspace=self.workspace, label=label, api_key=SK_KEY)


class DashboardTests(ZernioViewsTestsBase):
    def test_logged_out_redirects_to_login(self):
        self.client.logout()
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_non_member_gets_403(self):
        outsider = User.objects.create_user(
            email="outsider@example.com", password="x", tos_accepted_at=timezone.now()
        )
        self.client.force_login(outsider)
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.status_code, 403)

    def test_dashboard_renders_with_no_keys(self):
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Zernio")
        self.assertContains(response, "No Zernio API keys yet")

    def test_dashboard_shows_destination_for_bound_accounts(self):
        key, account, _binding = BindingActionTests._bound(self)
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.status_code, 200)
        # Bound YouTube account shows its destination, not "Zernio".
        self.assertContains(response, "YouTube")

    def test_dashboard_shows_masked_key_never_full_key(self):
        self._key()
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "sk_****bbbb")
        self.assertNotContains(response, SK_KEY)


class AddKeyTests(ZernioViewsTestsBase):
    def test_rejects_malformed_key_without_network(self):
        response = self.client.post(
            reverse("zernio:add_key", kwargs={"workspace_id": self.workspace.pk}),
            {"key_identity": "not-a-key"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ZernioApiKey.objects.count(), 0)

    @patch("apps.zernio.services.test_key", return_value=DISCOVERED)
    def test_add_key_tests_then_saves(self, mock_test):
        response = self.client.post(
            reverse("zernio:add_key", kwargs={"workspace_id": self.workspace.pk}),
            {"key_identity": "bk-owner@example.com|" + SK_KEY},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        key = ZernioApiKey.objects.get()
        assert key.label == "bk-owner@example.com"
        assert key.owner_email == "bk-owner@example.com"
        assert key.api_key == SK_KEY  # decrypted on read
        assert key.key_suffix == "bbbb"
        # Three read-only lookups in total: the pre-save key test, the
        # post-save auto-discover check, and the discover page render
        # itself (followed redirect).
        assert mock_test.call_count == 3
        mock_test.assert_called_with(SK_KEY)
        self.assertContains(response, "BeritaKenapa")
        self.assertContains(response, "Connect")

    @patch("apps.zernio.services.test_key", return_value=DISCOVERED)
    def test_duplicate_key_is_refused(self, mock_test):
        self._key()
        response = self.client.post(
            reverse("zernio:add_key", kwargs={"workspace_id": self.workspace.pk}),
            {"key_identity": "again@example.com|" + SK_KEY},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ZernioApiKey.objects.count(), 1)

class AutoDiscoverTests(ZernioViewsTestsBase):
    """add_key takes the operator straight to Discover (option 1)."""

    @patch("apps.zernio.services.test_key", return_value=DISCOVERED)
    def test_add_key_redirects_to_discover(self, mock_test):
        response = self.client.post(
            reverse("zernio:add_key", kwargs={"workspace_id": self.workspace.pk}),
            {"key_identity": "bk-owner@example.com|" + SK_KEY},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        key = ZernioApiKey.objects.get()
        discover_url = reverse(
            "zernio:discover",
            kwargs={"workspace_id": self.workspace.pk, "key_id": key.pk},
        )
        assert response.redirect_chain[0][0] == discover_url
        assert response.redirect_chain[0][1] == 302
        self.assertContains(response, "BeritaKenapa")
        self.assertContains(response, "Connect")
        # No auto-connect: nothing is bound just by adding the key.
        self.assertEqual(ZernioAccountBinding.objects.count(), 0)
        self.assertEqual(SocialAccount.objects.filter(platform="zernio").count(), 0)
        assert mock_test.call_count == 3

    @patch("apps.zernio.services.test_key")
    def test_add_key_discovery_failure_keeps_key_and_warns(self, mock_test):
        mock_test.side_effect = [DISCOVERED, ProviderError("Zernio timed out")]
        response = self.client.post(
            reverse("zernio:add_key", kwargs={"workspace_id": self.workspace.pk}),
            {"key_identity": "bk-owner@example.com|" + SK_KEY},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ZernioApiKey.objects.count(), 1)
        # Back on the dashboard, not Discover — the key survived.
        assert response.redirect_chain[0][0] == self.dashboard_url
        self.assertContains(response, "tersimpan, tapi daftar akun belum bisa diambil")
        self.assertEqual(ZernioAccountBinding.objects.count(), 0)
        assert mock_test.call_count == 2

    @patch("apps.zernio.services.test_key", return_value=[])
    def test_add_key_with_zero_accounts_still_goes_to_discover(self, _mock_test):
        response = self.client.post(
            reverse("zernio:add_key", kwargs={"workspace_id": self.workspace.pk}),
            {"key_identity": "bk-owner@example.com|" + SK_KEY},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        key = ZernioApiKey.objects.get()
        discover_url = reverse(
            "zernio:discover",
            kwargs={"workspace_id": self.workspace.pk, "key_id": key.pk},
        )
        assert response.redirect_chain[0][0] == discover_url
        self.assertContains(response, "No accounts under this key")

class DiscoverConnectTests(ZernioViewsTestsBase):
    @patch("apps.zernio.services.test_key", return_value=DISCOVERED)
    def test_discover_lists_accounts_with_connect(self, _mock_test):
        key = self._key()
        url = reverse("zernio:discover", kwargs={"workspace_id": self.workspace.pk, "key_id": key.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "BeritaKenapa")
        self.assertContains(response, "Connect")

    @patch("apps.zernio.services.test_key", return_value=DISCOVERED)
    def test_discover_shows_destination_labels(self, _mock_test):
        key = self._key()
        url = reverse("zernio:discover", kwargs={"workspace_id": self.workspace.pk, "key_id": key.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        # The two rows are YouTube and TikTok (the destination platforms),
        # not the transport name — both accounts are named BeritaKenapa.
        self.assertContains(response, "YouTube")
        self.assertContains(response, "TikTok")
        self.assertNotContains(response, "Youtube")

    def test_connect_creates_account_and_binding(self):
        key = self._key()
        response = self.client.post(
            reverse("zernio:connect_account", kwargs={"workspace_id": self.workspace.pk}),
            {
                "key_id": str(key.pk),
                "account_id": "acc-yt",
                "platform": "youtube",
                "username": "beritakenapa",
                "display_name": "BeritaKenapa",
                "profile_id": "prof1",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        account = SocialAccount.objects.get(platform="zernio")
        assert account.account_name == "BeritaKenapa"
        assert account.account_platform_id == "acc-yt"
        binding = ZernioAccountBinding.objects.get(social_account=account)
        assert binding.api_key_id == key.pk
        assert binding.destination_platform == "youtube"

    def test_connect_twice_is_refused(self):
        key = self._key()
        payload = {
            "key_id": str(key.pk),
            "account_id": "acc-yt",
            "platform": "youtube",
            "username": "beritakenapa",
            "display_name": "BeritaKenapa",
            "profile_id": "prof1",
        }
        url = reverse("zernio:connect_account", kwargs={"workspace_id": self.workspace.pk})
        self.client.post(url, payload, follow=True)
        self.client.post(url, payload, follow=True)
        self.assertEqual(SocialAccount.objects.filter(platform="zernio").count(), 1)


class BindingActionTests(ZernioViewsTestsBase):
    def _bound(self):
        key = self._key()
        account = SocialAccount.objects.create(
            workspace=self.workspace,
            platform="zernio",
            account_platform_id="acc-yt",
            account_name="BeritaKenapa",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        binding = ZernioAccountBinding.objects.create(
            social_account=account,
            api_key=key,
            zernio_account_id="acc-yt",
            zernio_profile_id="prof1",
            destination_platform="youtube",
        )
        return key, account, binding

    def test_unbind_disconnects_account(self):
        _key, account, binding = self._bound()
        response = self.client.post(
            reverse(
                "zernio:unbind_account",
                kwargs={"workspace_id": self.workspace.pk, "binding_id": binding.pk},
            ),
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ZernioAccountBinding.objects.filter(pk=binding.pk).exists())
        account.refresh_from_db()
        self.assertEqual(account.connection_status, SocialAccount.ConnectionStatus.DISCONNECTED)

    def test_disable_key_disconnects_bound_accounts(self):
        key, account, _binding = self._bound()
        response = self.client.post(
            reverse(
                "zernio:set_key_disabled",
                kwargs={"workspace_id": self.workspace.pk, "key_id": key.pk},
            ),
            {"disabled": "1"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        key.refresh_from_db()
        account.refresh_from_db()
        self.assertEqual(key.status, ZernioApiKey.Status.DISABLED)
        self.assertEqual(account.connection_status, SocialAccount.ConnectionStatus.DISCONNECTED)


class ReconnectAfterUnbindTests(ZernioViewsTestsBase):
    """Unbind then connect the same account again must succeed (no 500).

    ``unbind_account`` deletes the binding but keeps the DISCONNECTED
    ``SocialAccount`` row; connecting the same account again must reuse
    that row instead of creating a second one (unique triple).
    """

    def test_reconnect_after_unbind_reuses_account_row(self):
        key = self._key()
        payload = {
            "key_id": str(key.pk),
            "account_id": "acc-yt",
            "platform": "youtube",
            "username": "beritakenapa",
            "display_name": "BeritaKenapa",
            "profile_id": "prof1",
        }
        url = reverse("zernio:connect_account", kwargs={"workspace_id": self.workspace.pk})
        self.client.post(url, payload, follow=True)
        account = SocialAccount.objects.get(platform="zernio")
        binding = ZernioAccountBinding.objects.get(social_account=account)
        self.client.post(
            reverse(
                "zernio:unbind_account",
                kwargs={"workspace_id": self.workspace.pk, "binding_id": binding.pk},
            ),
            follow=True,
        )
        account.refresh_from_db()
        self.assertEqual(account.connection_status, SocialAccount.ConnectionStatus.DISCONNECTED)
        response = self.client.post(url, payload, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(SocialAccount.objects.filter(platform="zernio").count(), 1)
        account.refresh_from_db()
        self.assertEqual(account.connection_status, SocialAccount.ConnectionStatus.CONNECTED)
        self.assertTrue(ZernioAccountBinding.objects.filter(social_account=account).exists())
