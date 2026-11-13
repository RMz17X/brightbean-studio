"""Tests for the ``email|apikey`` key identity.

The email is the key's display identity in the UI; it is format-validated
only, because Zernio's API never returns the key owner's email. Keys saved
before the email field existed keep showing their label (fallback).
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
from ..services import KeyIdentityError, parse_key_identity

SK_KEY = "sk_" + "c" * 64
EMAIL = "owner@example.com"
IDENTITY = f"{EMAIL}|{SK_KEY}"

DISCOVERED = [
    {
        "account_id": "acc-yt",
        "platform": "youtube",
        "username": "beritakenapa",
        "display_name": "BeritaKenapa",
        "is_active": True,
        "profile_id": "prof1",
    },
]


class ParseKeyIdentityTests(TestCase):
    def test_valid_identity_splits_on_first_pipe(self):
        email, key = parse_key_identity(f"  {EMAIL} | {SK_KEY}  ")
        assert email == EMAIL
        assert key == SK_KEY

    def test_missing_separator_is_refused(self):
        with self.assertRaises(KeyIdentityError) as ctx:
            parse_key_identity(SK_KEY)
        self.assertIn("email|apikey", str(ctx.exception))

    def test_invalid_email_is_refused(self):
        with self.assertRaises(KeyIdentityError) as ctx:
            parse_key_identity(f"not-an-email|{SK_KEY}")
        self.assertIn("Email tidak valid", str(ctx.exception))

    def test_key_without_sk_prefix_is_refused(self):
        with self.assertRaises(KeyIdentityError):
            parse_key_identity(f"{EMAIL}|{'d' * 64}")

    def test_extra_pipe_in_key_part_is_refused(self):
        # Split is on the first '|', so the remainder is not a valid key.
        with self.assertRaises(KeyIdentityError):
            parse_key_identity(f"{EMAIL}|{SK_KEY}|extra")

    def test_empty_input_is_refused(self):
        with self.assertRaises(KeyIdentityError):
            parse_key_identity("")


class EmailIdentityViewsTests(TestCase):
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

    def _key(self, label="Key 1", email=""):
        return ZernioApiKey.objects.create(
            workspace=self.workspace, label=label, owner_email=email, api_key=SK_KEY
        )

    @patch("apps.zernio.services.test_key", return_value=DISCOVERED)
    def test_add_key_saves_email_as_label_and_identity(self, mock_test):
        response = self.client.post(
            reverse("zernio:add_key", kwargs={"workspace_id": self.workspace.pk}),
            {"key_identity": IDENTITY},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        key = ZernioApiKey.objects.get()
        assert key.owner_email == EMAIL
        assert key.label == EMAIL
        assert key.display_name == EMAIL
        assert key.api_key == SK_KEY
        # Three read-only lookups: pre-save test, post-save auto-discover
        # check, and the discover page render (followed redirect).
        assert mock_test.call_count == 3
        mock_test.assert_called_with(SK_KEY)

    def test_add_key_with_bad_format_saves_nothing(self):
        for bad in (SK_KEY, f"not-an-email|{SK_KEY}", f"{EMAIL}|nope"):
            response = self.client.post(
                reverse("zernio:add_key", kwargs={"workspace_id": self.workspace.pk}),
                {"key_identity": bad},
                follow=True,
            )
            self.assertEqual(response.status_code, 200)
        self.assertEqual(ZernioApiKey.objects.count(), 0)

    def test_dashboard_shows_email_as_card_title(self):
        self._key(email=EMAIL)
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, EMAIL)
        # The inline add form moved to the dedicated Add page: the
        # dashboard now links there instead of carrying the placeholder.
        self.assertContains(
            response,
            reverse("zernio:add_key", kwargs={"workspace_id": self.workspace.pk}),
        )

    def test_dashboard_falls_back_to_label_without_email(self):
        self._key(label="Legacy key")
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Legacy key")

    @patch("apps.zernio.services.test_key", return_value=DISCOVERED)
    def test_discover_title_shows_email(self, _mock_test):
        key = self._key(email=EMAIL)
        url = reverse("zernio:discover", kwargs={"workspace_id": self.workspace.pk, "key_id": key.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, EMAIL)

    def test_rebind_dropdown_shows_email_with_slots(self):
        key1 = self._key(label="K1", email="one@example.com")
        self._key(label="K2", email="two@example.com")
        account = SocialAccount.objects.create(
            workspace=self.workspace,
            platform="zernio",
            account_platform_id="acc-yt",
            account_name="BeritaKenapa",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        ZernioAccountBinding.objects.create(
            social_account=account,
            api_key=key1,
            zernio_account_id="acc-yt",
            zernio_profile_id="prof1",
            destination_platform="youtube",
        )
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "one@example.com")
        self.assertContains(response, "two@example.com (0/2)")
