"""Zernio key-pool service: assign, bind, rebind, unbind.

The one rule that matters: **a key holds at most 2 accounts, and the
binding is automatic**. When an account is connected, :func:`assign_key`
picks a key with a free slot (the key the accounts were discovered with
first, if it still has room); when a key reaches 2/2 it flips to FULL and
is never picked again. Keys are never copied onto accounts — the binding
row is the only link, so disabling a key takes effect immediately.

All slot decisions run inside a transaction with ``select_for_update`` on
the key rows, so two simultaneous connects cannot overfill a key.
"""

from __future__ import annotations

import logging
import re

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction

from apps.social_accounts.models import SocialAccount
from providers.zernio import MAX_ACCOUNTS_PER_KEY, ZernioProvider

from .models import ZernioAccountBinding, ZernioApiKey

logger = logging.getLogger(__name__)

# ``sk_`` + 64 hex characters, the format Zernio documents.
KEY_RE = re.compile(r"^sk_[0-9a-fA-F]{64}$")


class KeyIdentityError(ValueError):
    """A pasted ``email|apikey`` identity failed validation."""


def parse_key_identity(value: str) -> tuple[str, str]:
    """Split a pasted ``email|apikey`` identity into (email, key).

    The split is on the FIRST ``|``; anything after it must be exactly one
    ``sk_`` key, so a stray extra ``|`` fails the key check. The email is
    validated for format only — Zernio's API never returns the key owner's
    email, so it cannot be verified against Zernio.
    """
    raw = (value or "").strip()
    if "|" not in raw:
        raise KeyIdentityError(
            "Format harus email|apikey — contoh nama@email.com|sk_...."
        )
    email, _, key = raw.partition("|")
    email = email.strip()
    key = key.strip()
    try:
        validate_email(email)
    except ValidationError:
        raise KeyIdentityError(
            f"Email tidak valid: {email!r}. Format harus email|apikey — contoh nama@email.com|sk_...."
        ) from None
    if not KEY_RE.match(key):
        raise KeyIdentityError(
            "API key tidak valid — harus diawali sk_ diikuti 64 karakter hex."
        )
    return email, key


class NoFreeKeyError(Exception):
    """No key in the workspace pool has a free account slot."""


class AlreadyBoundError(Exception):
    """This Zernio account is already connected in this workspace."""


class KeyInUseError(Exception):
    """The key cannot be deleted while accounts are bound to it."""


def assign_key(workspace, *, prefer_key: ZernioApiKey | None = None) -> ZernioApiKey:
    """Pick a key with a free slot, or raise :class:`NoFreeKeyError`.

    ``prefer_key`` (the key the accounts were just discovered with) wins
    when it belongs to this workspace, is not disabled, and still has a
    slot. Otherwise the oldest key with room is picked. Callers must hold
    no transaction of their own — this opens one and locks the key rows.
    """
    with transaction.atomic():
        keys = list(
            ZernioApiKey.objects.for_workspace(workspace.id)
            .filter(status__in=[ZernioApiKey.Status.ACTIVE, ZernioApiKey.Status.FULL])
            .select_for_update()
            .order_by("created_at")
        )
        ordered: list[ZernioApiKey] = []
        if prefer_key is not None and prefer_key.workspace_id == workspace.id:
            ordered = [k for k in keys if k.pk == prefer_key.pk]
        ordered += [k for k in keys if k not in ordered]
        for key in ordered:
            if key.status == ZernioApiKey.Status.DISABLED:
                continue
            if key.bindings.count() < MAX_ACCOUNTS_PER_KEY:
                return key
    raise NoFreeKeyError(
        "All Zernio API keys in this workspace are full (2 accounts each). "
        "Add a new API key on the Zernio page first."
    )


def get_binding_for_account(social_account: SocialAccount) -> ZernioAccountBinding | None:
    """The key binding for a Zernio account, or None when unbound."""
    return getattr(social_account, "zernio_binding", None)


def credentials_for_account(social_account: SocialAccount) -> dict:
    """Provider credentials for a Zernio account, from its binding.

    Read by the publish engine (``_resolve_publish_credentials``). An
    unbound account yields ``{}`` and the provider then refuses to publish
    with a message that names the fix — it never falls back to a guess.
    """
    binding = get_binding_for_account(social_account)
    if binding is None:
        return {}
    return {
        "api_key": binding.api_key.api_key,
        "account_id": binding.zernio_account_id,
        "profile_id": binding.zernio_profile_id,
        "destination_platform": binding.destination_platform,
    }


def test_key(api_key_value: str) -> list[dict]:
    """Validate a raw key against Zernio: list its accounts.

    Raises the provider's error (401 = dead key) on failure. The value is
    never logged.
    """
    return ZernioProvider().list_accounts(api_key_value)


def connect_account(workspace, *, api_key: ZernioApiKey, account: dict) -> tuple[SocialAccount, ZernioAccountBinding]:
    """Create the SocialAccount + binding for one discovered Zernio account.

    ``account`` is one normalised dict from :meth:`ZernioProvider.list_accounts`.
    The key is (re-)picked by :func:`assign_key` with the discovery key
    preferred, so a connect never overfills a key even if the operator
    discovered with a key that filled up in the meantime. A row left
    DISCONNECTED by :func:`unbind_account` is reused instead of created,
    so reconnecting after an unbind cannot hit the unique
    (workspace, platform, account_platform_id) constraint.
    """
    with transaction.atomic():
        existing = ZernioAccountBinding.objects.filter(
            social_account__workspace=workspace,
            zernio_account_id=account["account_id"],
        ).select_related("social_account").first()
        if existing is not None:
            raise AlreadyBoundError(
                f"This Zernio account is already connected as {existing.social_account.account_name}."
            )
        key = assign_key(workspace, prefer_key=api_key)
        display_name = account.get("display_name") or account.get("username") or account["account_id"]
        # Reconnect after unbind: unbind_account deletes the binding but
        # keeps the DISCONNECTED SocialAccount row, and the
        # (workspace, platform, account_platform_id) triple is unique —
        # creating a second row for the same account would raise
        # IntegrityError. Reuse the disconnected row instead.
        social_account = SocialAccount.objects.filter(
            workspace=workspace,
            platform="zernio",
            account_platform_id=account["account_id"],
            connection_status=SocialAccount.ConnectionStatus.DISCONNECTED,
        ).first()
        if social_account is None:
            social_account = SocialAccount.objects.create(
                workspace=workspace,
                platform="zernio",
                account_platform_id=account["account_id"],
                account_name=display_name,
                account_handle=account.get("username") or "",
                connection_status=SocialAccount.ConnectionStatus.CONNECTED,
            )
        else:
            social_account.account_name = display_name
            social_account.account_handle = account.get("username") or ""
            social_account.connection_status = SocialAccount.ConnectionStatus.CONNECTED
            social_account.last_error = ""
            social_account.save(
                update_fields=["account_name", "account_handle", "connection_status", "last_error"]
            )
        binding = ZernioAccountBinding.objects.create(
            social_account=social_account,
            api_key=key,
            zernio_account_id=account["account_id"],
            zernio_profile_id=account.get("profile_id") or "",
            destination_platform=account["platform"],
        )
        key.refresh_status()
        logger.info("Connected Zernio account %s via key %s", display_name, key.display_name)
        return social_account, binding


def unbind_account(binding: ZernioAccountBinding) -> SocialAccount:
    """Drop the binding and disconnect the account. The key is untouched."""
    with transaction.atomic():
        key = binding.api_key
        account = binding.social_account
        binding.delete()
        account.connection_status = SocialAccount.ConnectionStatus.DISCONNECTED
        account.last_error = "Unbound from its Zernio API key. Rebind it from the Zernio page to publish again."
        account.save(update_fields=["connection_status", "last_error"])
        key.refresh_status()
        logger.info("Unbound Zernio account %s from key %s", account.account_name, key.display_name)
        return account


def rebind_account(binding: ZernioAccountBinding, new_key: ZernioApiKey) -> ZernioAccountBinding:
    """Move a binding to another key with a free slot."""
    if new_key.workspace_id != binding.api_key.workspace_id:
        raise NoFreeKeyError("That key belongs to a different workspace.")
    if new_key.status == ZernioApiKey.Status.DISABLED:
        raise NoFreeKeyError("That key is disabled.")
    with transaction.atomic():
        locked = ZernioApiKey.objects.select_for_update().get(pk=new_key.pk)
        if locked.bindings.count() >= MAX_ACCOUNTS_PER_KEY:
            raise NoFreeKeyError(f"Key {locked.display_name} is full (2/2 accounts). Pick another key.")
        old_key = binding.api_key
        binding.api_key = locked
        binding.save(update_fields=["api_key"])
        old_key.refresh_status()
        locked.refresh_status()
        logger.info(
            "Rebound Zernio account %s from key %s to key %s",
            binding.social_account.account_name,
            old_key.display_name,
            locked.display_name,
        )
        return binding


def set_key_disabled(key: ZernioApiKey, *, disabled: bool) -> ZernioApiKey:
    """Disable/enable a key, flagging its bound accounts accordingly.

    Disabling does not unbind anything: the accounts stay listed, marked
    disconnected with an error that names the fix, and resume automatically
    when the key is re-enabled.
    """
    with transaction.atomic():
        locked = ZernioApiKey.objects.select_for_update().get(pk=key.pk)
        if disabled:
            locked.status = ZernioApiKey.Status.DISABLED
            locked.save(update_fields=["status", "updated_at"])
            SocialAccount.objects.filter(zernio_binding__api_key=locked).update(
                connection_status=SocialAccount.ConnectionStatus.DISCONNECTED,
                last_error="Its Zernio API key is disabled. Re-enable the key on the Zernio page.",
            )
        else:
            locked.status = ZernioApiKey.Status.ACTIVE
            locked.save(update_fields=["status", "updated_at"])
            locked.refresh_status()
            SocialAccount.objects.filter(zernio_binding__api_key=locked).update(
                connection_status=SocialAccount.ConnectionStatus.CONNECTED,
                last_error="",
            )
        logger.info("Zernio key %s %s", locked.display_name, "disabled" if disabled else "enabled")
        return locked
