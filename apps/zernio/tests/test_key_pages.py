"""Tests for the Zernio Add + Edit key pages.

Only routing and rendering are exercised through the test client +
``reverse`` — views are never imported directly. Zernio itself is
patched out (``services.test_key``), so no network call is ever made.

Covers the ``zernio:add_key`` page (GET form + the legacy POST
behaviour) and the ``zernio:edit_key`` page (GET shows status/slots/
bound accounts but never the full key; POST only ever changes the
owner email and label, never the key value; keys from another
workspace are invisible here).
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

from ..models import ZernioAccountBinding, ZernioApiKey

SK_KEY = "sk_" + "b" * 64
OTHER_SK_KEY = "sk_" + "d" * 64
EMAIL = "owner@example.com"

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


class KeyPagesTestsBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="zernio-owner@example.com",
            password="test-password-123",
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
        self.add_url = reverse("zernio:add_key", kwargs={"workspace_id": self.workspace.pk})

    def _key(self, label="Key 1", email=EMAIL, key_value=SK_KEY, workspace=None):
        return ZernioApiKey.objects.create(
            workspace=workspace or self.workspace,
            label=label,
            owner_email=email,
            api_key=key_value,
        )

    def _bind(self, key, account_id, platform, name):
        account = SocialAccount.objects.create(
            workspace=self.workspace,
            platform="zernio",
            account_platform_id=account_id,
            account_name=name,
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        binding = ZernioAccountBinding.objects.create(
            social_account=account,
            api_key=key,
            zernio_account_id=account_id,
            zernio_profile_id="prof-" + account_id,
            destination_platform=platform,
        )
        return account, binding

    def _edit_url(self, key, workspace=None):
        return reverse(
            "zernio:edit_key",
            kwargs={"workspace_id": (workspace or self.workspace).pk, "key_id": key.pk},
        )

    def _other_workspace_key(self):
        other_org = Organization.objects.create(name="Other Org")
        other_workspace = Workspace.objects.create(organization=other_org, name="Other WS")
        return ZernioApiKey.objects.create(
            workspace=other_workspace,
            label="Other key",
            owner_email="other@example.com",
            api_key="sk_" + "e" * 64,
        )


class AddKeyPageTests(KeyPagesTestsBase):
    def test_add_page_renders_key_identity_form(self):
        response = self.client.get(self.add_url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "zernio/add_key.html")
        self.assertContains(response, 'name="key_identity"')
        self.assertContains(response, "nama@email.com|sk_...")

    def test_add_rejects_malformed_identity(self):
        response = self.client.post(self.add_url, {"key_identity": "not-a-key"}, follow=True)
        self.assertEqual(response.status_code, 200)
        assert response.redirect_chain[0][0] == self.dashboard_url
        self.assertEqual(ZernioApiKey.objects.count(), 0)

    def test_add_duplicate_key_is_refused(self):
        self._key()
        with patch("apps.zernio.services.test_key", return_value=DISCOVERED):
            response = self.client.post(
                self.add_url,
                {"key_identity": "again@example.com|" + SK_KEY},
                follow=True,
            )
        self.assertEqual(response.status_code, 200)
        assert response.redirect_chain[0][0] == self.dashboard_url
        self.assertEqual(ZernioApiKey.objects.count(), 1)

    @patch("apps.zernio.services.test_key", return_value=DISCOVERED)
    def test_add_valid_key_redirects_to_discover(self, mock_test):
        response = self.client.post(
            self.add_url,
            {"key_identity": EMAIL + "|" + SK_KEY},
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
        assert key.owner_email == EMAIL
        assert key.label == EMAIL
        assert key.api_key == SK_KEY  # decrypted on read
        assert key.key_suffix == "bbbb"
        mock_test.assert_called_with(SK_KEY)


class EditKeyPageTests(KeyPagesTestsBase):
    def _full_key(self):
        key = self._key()
        self._bind(key, "acc-yt", "youtube", "BeritaKenapa")
        self._bind(key, "acc-tt", "tiktok", "BeritaTikTok")
        key.refresh_status()
        key.refresh_from_db()
        assert key.status == ZernioApiKey.Status.FULL
        assert key.slots_used == 2
        return key

    def test_edit_page_shows_status_slots_and_bound_accounts(self):
        key = self._full_key()
        response = self.client.get(self._edit_url(key))
        self.assertEqual(response.status_code, 200)
        # Slot usage for a FULL key, and its status.
        self.assertContains(response, "2/2")
        self.assertIn("full", response.content.decode().lower())
        # Both bound accounts are named.
        self.assertContains(response, "BeritaKenapa")
        self.assertContains(response, "BeritaTikTok")
        # The form is prefilled with the current identity.
        self.assertContains(response, EMAIL)

    def test_edit_page_never_shows_full_key(self):
        key = self._full_key()
        response = self.client.get(self._edit_url(key))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, SK_KEY)
        # Only the masked form / last-4 suffix may appear.
        self.assertContains(response, "bbbb")

    def test_edit_valid_post_updates_email_and_label(self):
        key = self._key()
        response = self.client.post(
            self._edit_url(key),
            {"owner_email": "new-owner@example.com", "label": "Renamed key"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        assert response.redirect_chain[0][0] == self.dashboard_url
        key.refresh_from_db()
        assert key.owner_email == "new-owner@example.com"
        assert key.label == "Renamed key"
        # A success message was shown on the dashboard.
        messages = list(response.context["messages"])
        assert messages, "expected a success message after a valid edit"
        assert any(m.level_tag == "success" for m in messages)
        # The key value itself is untouched by a plain edit.
        assert key.api_key == SK_KEY
        assert key.key_suffix == "bbbb"

    def test_edit_invalid_email_is_rejected_without_changes(self):
        key = self._key()
        response = self.client.post(
            self._edit_url(key),
            {"owner_email": "not-an-email", "label": "Should Not Change"},
        )
        # Rejected: stays on the edit page instead of redirecting.
        self.assertEqual(response.status_code, 200)
        key.refresh_from_db()
        assert key.owner_email == EMAIL
        assert key.label == "Key 1"

    def test_edit_post_cannot_change_key_value(self):
        key = self._key()
        response = self.client.post(
            self._edit_url(key),
            {
                "owner_email": EMAIL,
                "label": "Key 1",
                "api_key": OTHER_SK_KEY,
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        key.refresh_from_db()
        assert key.key_suffix == "bbbb"
        assert key.api_key == SK_KEY  # decrypted on read; not the posted value
        assert key.api_key != OTHER_SK_KEY

    def test_edit_key_from_other_workspace_get_returns_404(self):
        other_key = self._other_workspace_key()
        response = self.client.get(self._edit_url(other_key))
        self.assertEqual(response.status_code, 404)

    def test_edit_key_from_other_workspace_post_returns_404(self):
        other_key = self._other_workspace_key()
        response = self.client.post(
            self._edit_url(other_key),
            {"owner_email": "intruder@example.com", "label": "Hijacked"},
        )
        self.assertEqual(response.status_code, 404)
        other_key.refresh_from_db()
        assert other_key.owner_email == "other@example.com"
        assert other_key.label == "Other key"
