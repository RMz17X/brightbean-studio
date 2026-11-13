"""Tests for hiding the Rebind form when no other key can take the account.

With a single key in the pool there is no rebind destination, so the
dashboard must not show a Rebind form with an empty dropdown. The form
returns (with its normal behaviour) as soon as another key with a free
slot exists. Pool logic itself (auto-assign, FULL lock, rebind) is
unchanged and covered by ``test_pool.py``.
"""

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

from ..models import ZernioAccountBinding, ZernioApiKey

SK_KEY_1 = "sk_" + "b" * 64
SK_KEY_2 = "sk_" + "c" * 64


class RebindVisibilityTests(TestCase):
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

    def _key(self, label, api_key):
        return ZernioApiKey.objects.create(
            workspace=self.workspace, label=label, api_key=api_key
        )

    def _bind(self, key, account_id, platform="youtube"):
        account = SocialAccount.objects.create(
            workspace=self.workspace,
            platform="zernio",
            account_platform_id=account_id,
            account_name=f"Account {account_id}",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        binding = ZernioAccountBinding.objects.create(
            social_account=account,
            api_key=key,
            zernio_account_id=account_id,
            zernio_profile_id="prof1",
            destination_platform=platform,
        )
        return account, binding

    def test_single_key_hides_rebind_form_but_keeps_account_and_unbind(self):
        key = self._key("Key 1", SK_KEY_1)
        account, _binding = self._bind(key, "acc-yt")
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.status_code, 200)
        # No Rebind form at all (no empty dropdown, no submit button).
        self.assertNotContains(response, "rebind_account")
        self.assertNotContains(response, ">Rebind</button>")
        self.assertContains(response, "Add another API key with a free slot")
        # The bound account itself still renders, with Unbind available.
        self.assertContains(response, account.account_name)
        self.assertContains(response, ">Unbind</button>")

    def test_two_keys_shows_rebind_form_and_rebind_still_works(self):
        key1 = self._key("Key 1", SK_KEY_1)
        key2 = self._key("Key 2", SK_KEY_2)
        account, binding = self._bind(key1, "acc-yt")
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, ">Rebind</button>")
        self.assertContains(response, "Key 2 (0/2)")
        # Rebind itself behaves exactly as before.
        response = self.client.post(
            reverse(
                "zernio:rebind_account",
                kwargs={"workspace_id": self.workspace.pk, "binding_id": binding.pk},
            ),
            {"key_id": str(key2.pk)},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        binding.refresh_from_db()
        self.assertEqual(binding.api_key_id, key2.pk)
        self.assertContains(response, "is now bound to key")

    def test_other_key_full_hides_rebind_form(self):
        key1 = self._key("Key 1", SK_KEY_1)
        key2 = self._key("Key 2", SK_KEY_2)
        self._bind(key2, "acc-a")
        self._bind(key2, "acc-b", platform="tiktok")
        _account, binding1 = self._bind(key1, "acc-yt")
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.status_code, 200)
        # Key 2 is 2/2, so it is not a usable destination for key1's
        # account: no Rebind form may target key1's binding.
        url1 = reverse(
            "zernio:rebind_account",
            kwargs={"workspace_id": self.workspace.pk, "binding_id": binding1.pk},
        )
        self.assertNotContains(response, url1)
        # ...but key1 still has a free slot, so key2's two accounts CAN
        # rebind to it -- exactly two Rebind forms render (one per key2
        # binding), none for key1's account.
        self.assertContains(response, ">Rebind</button>", count=2)

    def test_other_key_disabled_hides_rebind_form(self):
        key1 = self._key("Key 1", SK_KEY_1)
        key2 = self._key("Key 2", SK_KEY_2)
        key2.status = ZernioApiKey.Status.DISABLED
        key2.save(update_fields=["status"])
        _account, _binding = self._bind(key1, "acc-yt")
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "rebind_account")
        self.assertNotContains(response, ">Rebind</button>")
