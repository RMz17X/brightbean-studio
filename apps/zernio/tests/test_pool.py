"""Tests for the Zernio key pool: auto-assign, lock at 2/2, rebind, disable.

No Zernio call is ever made here — the pool logic only touches local rows.
"""

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import User
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

from ..models import ZernioAccountBinding, ZernioApiKey
from ..services import (
    AlreadyBoundError,
    NoFreeKeyError,
    assign_key,
    connect_account,
    credentials_for_account,
    rebind_account,
    set_key_disabled,
    unbind_account,
)


def _account_dict(account_id, platform="youtube", **kwargs):
    data = {
        "account_id": account_id,
        "platform": platform,
        "username": f"user-{account_id}",
        "display_name": f"Account {account_id}",
        "profile_id": "prof1",
    }
    data.update(kwargs)
    return data


class ZernioPoolTestsBase(TestCase):
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

    def _key(self, label="Key 1"):
        return ZernioApiKey.objects.create(
            workspace=self.workspace, label=label, api_key="sk_" + "a" * 64
        )


class AssignKeyTests(ZernioPoolTestsBase):
    def test_prefers_discovery_key_then_rolls_over_when_full(self):
        key1 = self._key("Key 1")
        key2 = self._key("Key 2")
        connect_account(self.workspace, api_key=key1, account=_account_dict("acc1"))
        connect_account(self.workspace, api_key=key1, account=_account_dict("acc2", platform="tiktok"))
        key1.refresh_from_db()
        assert key1.status == ZernioApiKey.Status.FULL
        assert key1.slots_used == 2
        assert key1.slots_free == 0
        # Third account discovered with key1 must roll over to key2.
        _sa, binding = connect_account(self.workspace, api_key=key1, account=_account_dict("acc3"))
        assert binding.api_key_id == key2.pk
        key2.refresh_from_db()
        assert key2.slots_used == 1

    def test_raises_when_every_key_is_full(self):
        key1 = self._key("Key 1")
        connect_account(self.workspace, api_key=key1, account=_account_dict("acc1"))
        connect_account(self.workspace, api_key=key1, account=_account_dict("acc2"))
        with self.assertRaises(NoFreeKeyError):
            assign_key(self.workspace)

    def test_raises_when_no_keys_exist(self):
        with self.assertRaises(NoFreeKeyError):
            assign_key(self.workspace)

    def test_disabled_key_is_never_picked(self):
        key1 = self._key("Key 1")
        set_key_disabled(key1, disabled=True)
        with self.assertRaises(NoFreeKeyError):
            assign_key(self.workspace, prefer_key=key1)


class BindUnbindTests(ZernioPoolTestsBase):
    def test_duplicate_account_is_refused(self):
        key1 = self._key("Key 1")
        connect_account(self.workspace, api_key=key1, account=_account_dict("acc1"))
        with self.assertRaises(AlreadyBoundError):
            connect_account(self.workspace, api_key=key1, account=_account_dict("acc1"))

    def test_credentials_come_from_the_binding(self):
        key1 = self._key("Key 1")
        social_account, _binding = connect_account(
            self.workspace, api_key=key1, account=_account_dict("acc1")
        )
        creds = credentials_for_account(social_account)
        assert creds["account_id"] == "acc1"
        assert creds["profile_id"] == "prof1"
        assert creds["destination_platform"] == "youtube"
        assert creds["api_key"] == "sk_" + "a" * 64

    def test_unbound_account_yields_no_credentials(self):
        account = SocialAccount.objects.create(
            workspace=self.workspace,
            platform="zernio",
            account_platform_id="ghost",
            account_name="Ghost",
        )
        assert credentials_for_account(account) == {}

    def test_unbind_disconnects_and_frees_the_slot(self):
        key1 = self._key("Key 1")
        _sa, binding = connect_account(self.workspace, api_key=key1, account=_account_dict("acc1"))
        assert key1.slots_used == 1
        account = unbind_account(binding)
        assert account.connection_status == SocialAccount.ConnectionStatus.DISCONNECTED
        assert not ZernioAccountBinding.objects.filter(pk=binding.pk).exists()
        key1.refresh_from_db()
        assert key1.status == ZernioApiKey.Status.ACTIVE
        assert key1.slots_used == 0

    def test_rebind_moves_account_and_updates_both_keys(self):
        key1 = self._key("Key 1")
        key2 = self._key("Key 2")
        _sa, binding = connect_account(self.workspace, api_key=key1, account=_account_dict("acc1"))
        rebind_account(binding, key2)
        binding.refresh_from_db()
        assert binding.api_key_id == key2.pk
        key1.refresh_from_db()
        key2.refresh_from_db()
        assert key1.slots_used == 0
        assert key2.slots_used == 1

    def test_rebind_to_full_key_is_refused(self):
        key1 = self._key("Key 1")
        key2 = self._key("Key 2")
        connect_account(self.workspace, api_key=key2, account=_account_dict("acc1"))
        connect_account(self.workspace, api_key=key2, account=_account_dict("acc2"))
        _sa, binding = connect_account(self.workspace, api_key=key1, account=_account_dict("acc3"))
        with self.assertRaises(NoFreeKeyError):
            rebind_account(binding, key2)

    def test_disable_disconnects_and_enable_reconnects(self):
        key1 = self._key("Key 1")
        social_account, _binding = connect_account(
            self.workspace, api_key=key1, account=_account_dict("acc1")
        )
        set_key_disabled(key1, disabled=True)
        social_account.refresh_from_db()
        assert social_account.connection_status == SocialAccount.ConnectionStatus.DISCONNECTED
        set_key_disabled(key1, disabled=False)
        social_account.refresh_from_db()
        assert social_account.connection_status == SocialAccount.ConnectionStatus.CONNECTED


class KeyModelTests(ZernioPoolTestsBase):
    def test_key_suffix_and_masking(self):
        key = self._key("Key 1")
        assert key.key_suffix == "aaaa"
        assert key.masked_key == "sk_****aaaa"
        # The full key must never appear in the model's string form.
        assert "sk_" + "a" * 64 not in str(key)
