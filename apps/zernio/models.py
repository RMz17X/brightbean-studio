"""Zernio key pool + account bindings.

One Zernio API key reaches at most 2 connected accounts (Zernio's free
tier). The pool is the single source of truth for keys: accounts are
*bound* to a key, never given a copy of it, so disabling or rotating a key
takes effect everywhere at once. ``SocialAccount`` rows for Zernio
destinations carry ``platform="zernio"``; the destination platform
(``youtube``/``tiktok``/...) and the Zernio account/profile ids live on
the binding, and the publish engine injects them into the provider's
credentials at publish time.
"""

from __future__ import annotations

import uuid

from django.db import models
from django.utils import timezone

from apps.common.encryption import EncryptedTextField
from apps.common.managers import WorkspaceScopedManager

# Zernio platform slugs, as returned by GET /v1/accounts, with labels.
DESTINATION_CHOICES: list[tuple[str, str]] = [
    ("youtube", "YouTube"),
    ("tiktok", "TikTok"),
    ("facebook", "Facebook"),
    ("instagram", "Instagram"),
    ("threads", "Threads"),
    ("pinterest", "Pinterest"),
    ("bluesky", "Bluesky"),
    ("google_business", "Google Business"),
    ("linkedin", "LinkedIn"),
]

DESTINATION_LABELS: dict[str, str] = dict(DESTINATION_CHOICES)
# Display-only fallback: an unbound Zernio account (e.g. after unbind, left
# DISCONNECTED with no binding) has display_platform "zernio", which is not
# a destination — label it "Zernio" instead of leaking the raw slug.
# NOT added to DESTINATION_CHOICES: "zernio" is a transport, never a
# binding destination.
DESTINATION_LABELS["zernio"] = "Zernio"


class ZernioApiKey(models.Model):
    """One Zernio ``sk_`` API key in a workspace's pool.

    The full key is stored encrypted and is never shown again after it is
    saved (Zernio itself behaves the same way). Only the last 4 characters
    are kept in cleartext for display.

    Identity: a key is identified by the Zernio login email of its owner
    (``owner_email``), in the ``email|apikey`` format the operator already
    uses. The email is a label only — Zernio's API never returns the key
    owner's email, so it cannot be verified against Zernio. ``label`` stays
    as the fallback display name for keys saved before the email field
    existed. The email is deliberately NOT unique: one Zernio login can
    own several API keys, each with its own 2-account slots.
    """

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        FULL = "full", "Full — 2/2 accounts"
        DISABLED = "disabled", "Disabled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="zernio_api_keys",
    )
    label = models.CharField(max_length=100, help_text="e.g. BeritaKenapa key 1")
    owner_email = models.EmailField(
        max_length=254,
        blank=True,
        default="",
        help_text="Zernio login email of the key owner — display identity only, not verified against Zernio.",
    )
    api_key = EncryptedTextField(help_text="Zernio API key (sk_ + 64 hex). Shown once, then never again.")
    key_suffix = models.CharField(max_length=4, editable=False, help_text="Last 4 characters, for display only.")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    last_tested_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "zernio_api_key"
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"{self.display_name} (****{self.key_suffix}, {self.slots_used}/2)"

    @property
    def display_name(self) -> str:
        """Identity shown in the UI: owner email, or label for legacy keys."""
        return self.owner_email or self.label

    def save(self, *args, **kwargs):
        if self.api_key:
            self.key_suffix = str(self.api_key)[-4:]
        super().save(*args, **kwargs)

    @property
    def masked_key(self) -> str:
        """Display form of the key — never the full value."""
        return f"sk_****{self.key_suffix}" if self.key_suffix else "****"

    @property
    def slots_used(self) -> int:
        return self.bindings.count()

    @property
    def slots_free(self) -> int:
        from providers.zernio import MAX_ACCOUNTS_PER_KEY

        return max(0, MAX_ACCOUNTS_PER_KEY - self.slots_used)

    @property
    def is_full(self) -> bool:
        return self.slots_free == 0

    def refresh_status(self) -> None:
        """Recompute ACTIVE/FULL from bindings. DISABLED is sticky."""
        if self.status == self.Status.DISABLED:
            return
        new_status = self.Status.FULL if self.is_full else self.Status.ACTIVE
        if new_status != self.status:
            self.status = new_status
            self.save(update_fields=["status", "updated_at"])


class ZernioAccountBinding(models.Model):
    """One BrightBean account bound to one pooled Zernio key.

    The binding is what the publish engine reads: it turns a
    ``SocialAccount(platform="zernio")`` into the API key + Zernio account
    id + profile id the provider needs. Unbinding deletes this row (and
    disconnects the account); the key itself is untouched.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    social_account = models.OneToOneField(
        "social_accounts.SocialAccount",
        on_delete=models.CASCADE,
        related_name="zernio_binding",
    )
    api_key = models.ForeignKey(
        ZernioApiKey,
        on_delete=models.PROTECT,
        related_name="bindings",
        help_text="Deleting a key with bound accounts is blocked; disable it instead.",
    )
    zernio_account_id = models.CharField(max_length=255, help_text="Zernio account _id.")
    zernio_profile_id = models.CharField(max_length=255, help_text="Zernio profile _id.")
    destination_platform = models.CharField(
        max_length=30,
        choices=DESTINATION_CHOICES,
        help_text="Zernio platform slug for this account (youtube, tiktok, ...).",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "zernio_account_binding"

    def __str__(self) -> str:
        return f"{self.social_account} -> {self.api_key.display_name} ({self.destination_platform})"

    @property
    def destination_label(self) -> str:
        return DESTINATION_LABELS.get(self.destination_platform, self.destination_platform)
