"""Zernio key-pool UI: add/test keys, discover accounts, bind/unbind.

One page per workspace (``/workspace/<id>/zernio/``). Auth mirrors the
rest of the app: ``login_required`` plus a workspace-membership check
(``_get_workspace``, same as the Live monitor). A Zernio API key is shown
exactly once — on the form that creates it — and never again afterwards,
only its last 4 characters.

All Zernio calls here are read-only (profiles/accounts) except the
operator's explicit Connect, which only creates local rows.
"""

from __future__ import annotations

import logging
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import prefetch_related_objects
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from django_ratelimit.decorators import ratelimit

from apps.members.models import WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace
from providers.exceptions import ProviderError

from . import services
from .models import DESTINATION_LABELS, ZernioAccountBinding, ZernioApiKey

logger = logging.getLogger(__name__)


def _get_workspace(request, workspace_id):
    """Resolve workspace and enforce membership check (Live-monitor pattern)."""
    workspace = get_object_or_404(Workspace, id=workspace_id)
    if not request.user.is_authenticated:
        raise PermissionDenied("Authentication required.")
    if not WorkspaceMembership.objects.filter(user=request.user, workspace=workspace).exists():
        raise PermissionDenied("You are not a member of this workspace.")
    return workspace


def _get_key(request, workspace, key_id):
    # ``key_id`` comes from the URL converter for most callers, but
    # connect/rebind read it from POST data — a non-UUID value raises
    # ValidationError instead of DoesNotExist, so map it to 404 here.
    try:
        return get_object_or_404(ZernioApiKey, id=key_id, workspace=workspace)
    except ValidationError:
        raise Http404("No Zernio API key matches the given id.") from None


def _keys_with_bindings(workspace):
    return list(
        ZernioApiKey.objects.for_workspace(workspace.id)
        .prefetch_related("bindings__social_account")
        .order_by("created_at")
    )


def _zernio_accounts(workspace):
    return list(
        SocialAccount.objects.for_workspace(workspace.id)
        .filter(platform="zernio")
        .select_related("zernio_binding__api_key")
        .order_by("account_name")
    )


# ----------------------------------------------------------------------
# Dashboard + keys
# ----------------------------------------------------------------------


def _attach_rebind_targets(keys):
    """Per key, the other keys a bound account could actually move to.

    A key is a rebind target only when it is a different key, not
    disabled, and still has a free slot (the same conditions
    ``services.rebind_account`` enforces). With a single key in the pool
    -- or when every other key is disabled / full -- the list is empty
    and the template hides the Rebind form instead of showing a dropdown
    with no usable destination.
    """
    for key in keys:
        key.rebind_targets = [
            other
            for other in keys
            if other.id != key.id
            and other.status != ZernioApiKey.Status.DISABLED
            and other.slots_free > 0
        ]
    return keys


@login_required
def zernio_dashboard(request, workspace_id):
    """Key pool: keys with slot usage, bound accounts, add-key form."""
    workspace = _get_workspace(request, workspace_id)
    return render(
        request,
        "zernio/keys.html",
        {
            "workspace": workspace,
            "workspace_id": workspace.id,
            "keys": _attach_rebind_targets(_keys_with_bindings(workspace)),
            "accounts": _zernio_accounts(workspace),
        },
    )


def _pool_stats(keys):
    """Pool totals: keys, slots used/free (max 2 accounts per key)."""
    from providers.zernio import MAX_ACCOUNTS_PER_KEY

    total_keys = len(keys)
    total_slots = total_keys * MAX_ACCOUNTS_PER_KEY
    slots_used = sum(key.slots_used for key in keys)
    return {
        "total_keys": total_keys,
        "total_slots": total_slots,
        "slots_used": slots_used,
        "slots_free": total_slots - slots_used,
    }


@login_required
@ratelimit(key="user", rate="10/m", method="POST", block=True)
def add_key(request, workspace_id):
    """Save a new key after testing it against Zernio (read-only).

    The operator pastes one line in the ``email|apikey`` format they
    already use elsewhere: the Zernio login email, then ``|``, then the
    ``sk_`` key. The email is the key's identity in the UI; it is format
    validated only — Zernio's API never returns the key owner's email.

    GET renders the dedicated add-key page; POST saves a new key after
    testing it against Zernio (read-only).

    On success the operator goes straight to the key's Discover page with
    its accounts listed (auto-discover); Connect stays manual per account.
    """
    workspace = _get_workspace(request, workspace_id)
    if request.method == "GET":
        keys = _keys_with_bindings(workspace)
        return render(
            request,
            "zernio/add_key.html",
            {
                "workspace": workspace,
                "workspace_id": workspace.id,
                "keys": keys,
                **_pool_stats(keys),
            },
        )
    try:
        email, raw_key = services.parse_key_identity(request.POST.get("key_identity"))
    except services.KeyIdentityError as exc:
        messages.error(request, str(exc))
        return redirect("zernio:dashboard", workspace_id=workspace.id)
    label = email[:100]
    # Encrypted at rest with a random nonce, so the DB cannot be filtered
    # for a duplicate — compare decrypted values in Python (tiny pool).
    for existing in ZernioApiKey.objects.for_workspace(workspace.id).only("api_key"):
        if existing.api_key == raw_key:
            messages.error(request, "This API key is already in the pool.")
            return redirect("zernio:dashboard", workspace_id=workspace.id)
    try:
        services.test_key(raw_key)  # raises on a dead key; result unused here
    except ProviderError as exc:
        logger.warning("Zernio key test failed for workspace %s: %s", workspace.id, exc)
        messages.error(request, f"Zernio rejected the key: {exc}")
        return redirect("zernio:dashboard", workspace_id=workspace.id)
    with transaction.atomic():
        key = ZernioApiKey.objects.create(
            workspace=workspace,
            label=label,
            owner_email=email,
            api_key=raw_key,
            last_tested_at=timezone.now(),
        )
    # Auto-discover: the key is verified already, so take the operator
    # straight to its accounts instead of making them press Discover by
    # hand. The discover page re-runs the same read-only lookup when it
    # renders, so this check costs one extra GET in total.
    try:
        discovered = services.test_key(raw_key)
    except ProviderError as exc:
        logger.warning(
            "Zernio discovery failed right after adding key for workspace %s: %s",
            workspace.id,
            exc,
        )
        messages.warning(
            request,
            f"Key {email} tersimpan, tapi daftar akun belum bisa diambil — pencet Discover accounts untuk mencoba lagi.",
        )
        return redirect("zernio:dashboard", workspace_id=workspace.id)
    messages.success(
        request,
        f"Key {email} added and verified — found {len(discovered)} account(s) under it. "
        "Connect the ones you want to use.",
    )
    return redirect("zernio:discover", workspace_id=workspace.id, key_id=key.pk)


# ----------------------------------------------------------------------
# Edit key identity
# ----------------------------------------------------------------------


# Edit halaman untuk satu Zernio API key (identitas saja).
#
# Batasan halaman ini — sengaja, dan ditulis juga di template:
#
# * Yang boleh diubah hanya ``owner_email`` dan ``label`` (identitas tampilan
#   key). Nilai ``api_key`` TIDAK PERNAH dibaca, ditampilkan, atau diubah di
#   sini: Zernio hanya menampilkan key sekali saat dibuat, dan BrightBean
#   pun menyimpannya terenkripsi dan hanya menampilkan 4 karakter terakhir
#   (``masked_key`` / ``key_suffix``).
# * Rotasi nilai key TIDAK didukung di halaman ini. Untuk mengganti nilai
#   key: hapus key lama dari dashboard, lalu tambah key baru (``add_key``).
# * Edit identitas tidak mengubah ``status`` atau slot key: key DISABLED
#   tetap DISABLED, dan jumlah akun terikat tidak berubah.
#


def _edit_context(request, workspace, key, submitted_owner_email=None, submitted_label=None):
    """Context bersama GET dan POST-gagal-validasi."""
    return {
        "workspace": workspace,
        "workspace_id": workspace.id,
        "key": key,
        "submitted_owner_email": submitted_owner_email,
        "submitted_label": submitted_label,
    }


@login_required
def edit_key(request, workspace_id, key_id):
    """Edit identitas satu Zernio API key: ``owner_email`` + ``label`` saja.

    GET: tampilkan halaman edit dengan data key saat ini (key diambil via
    ``_get_key`` — key milik workspace lain menghasilkan 404).

    POST: validasi ``owner_email`` (wajib, format email valid) dan ``label``
    (wajib, maks. 100 karakter). Bila tidak valid, render ulang halaman
    dengan pesan error dan TANPA menyimpan apa pun. Bila valid, simpan
    HANYA ``owner_email`` + ``label`` (``update_fields``) lalu redirect ke
    dashboard dengan pesan sukses.

    Nilai ``api_key``, ``status``, dan slot tidak pernah disentuh di sini.
    """
    workspace = _get_workspace(request, workspace_id)
    key = _get_key(request, workspace, key_id)
    # Satu query untuk daftar akun terikat beserta SocialAccount-nya.
    prefetch_related_objects([key], "bindings__social_account")

    if request.method != "POST":
        return render(request, "zernio/edit_key.html", _edit_context(request, workspace, key))

    owner_email = (request.POST.get("owner_email") or "").strip()
    label = (request.POST.get("label") or "").strip()

    errors = []
    if not owner_email:
        errors.append("Email pemilik wajib diisi.")
    else:
        try:
            validate_email(owner_email)
        except ValidationError:
            errors.append("Format email pemilik tidak valid.")
    if not label:
        errors.append("Label wajib diisi.")
    elif len(label) > 100:
        errors.append("Label maksimal 100 karakter.")

    if errors:
        for error in errors:
            messages.error(request, error)
        return render(
            request,
            "zernio/edit_key.html",
            _edit_context(
                request,
                workspace,
                key,
                submitted_owner_email=owner_email,
                submitted_label=label,
            ),
        )

    # Hanya identitas yang berubah — api_key, status, dan slot tidak disentuh.
    key.owner_email = owner_email
    key.label = label
    key.save(update_fields=["owner_email", "label", "updated_at"])
    messages.success(request, f"API key {key.display_name} berhasil diperbarui.")
    return redirect("zernio:dashboard", workspace_id=workspace.id)


@login_required
@require_POST
@ratelimit(key="user", rate="10/m", method="POST", block=True)
def test_key(request, workspace_id, key_id):
    """Re-test a stored key (read-only) and record the outcome."""
    workspace = _get_workspace(request, workspace_id)
    key = _get_key(request, workspace, key_id)
    try:
        accounts = services.test_key(key.api_key)
    except ProviderError as exc:
        key.last_error = str(exc)[:500]
        key.save(update_fields=["last_error"])
        messages.error(request, f"Zernio rejected key {key.display_name}: {exc}")
        return redirect("zernio:dashboard", workspace_id=workspace.id)
    key.last_tested_at = timezone.now()
    key.last_error = ""
    key.save(update_fields=["last_tested_at", "last_error"])
    messages.success(request, f"Key {key.display_name} works — Zernio reports {len(accounts)} connected account(s).")
    return redirect("zernio:dashboard", workspace_id=workspace.id)


@login_required
@require_POST
def set_key_disabled(request, workspace_id, key_id):
    """Disable/enable a key; bound accounts are flagged, not unbound."""
    workspace = _get_workspace(request, workspace_id)
    key = _get_key(request, workspace, key_id)
    disabled = request.POST.get("disabled") == "1"
    services.set_key_disabled(key, disabled=disabled)
    if disabled:
        messages.warning(request, f"Key {key.display_name} disabled. Its accounts are disconnected until the key is re-enabled.")
    else:
        messages.success(request, f"Key {key.display_name} re-enabled.")
    return redirect("zernio:dashboard", workspace_id=workspace.id)


@login_required
@require_POST
def delete_key(request, workspace_id, key_id):
    """Delete a key — only when nothing is bound to it."""
    workspace = _get_workspace(request, workspace_id)
    key = _get_key(request, workspace, key_id)
    if key.bindings.exists():
        messages.error(request, f"Key {key.display_name} still has bound accounts. Unbind or rebind them first.")
        return redirect("zernio:dashboard", workspace_id=workspace.id)
    identity = key.display_name
    key.delete()
    messages.success(request, f"Key {identity} deleted.")
    return redirect("zernio:dashboard", workspace_id=workspace.id)


# ----------------------------------------------------------------------
# Discovery + connect
# ----------------------------------------------------------------------


@login_required
@require_GET
@ratelimit(key="user", rate="30/m", method="GET", block=True)
def discover(request, workspace_id, key_id):
    """Accounts visible under one key, with Connect buttons.

    Read-only against Zernio. Accounts already connected in this workspace
    are marked and cannot be connected twice.
    """
    workspace = _get_workspace(request, workspace_id)
    key = _get_key(request, workspace, key_id)
    try:
        accounts = services.test_key(key.api_key)
    except ProviderError as exc:
        messages.error(request, f"Could not list accounts for key {key.display_name}: {exc}")
        return redirect("zernio:dashboard", workspace_id=workspace.id)
    bound_ids = set(
        ZernioAccountBinding.objects.filter(social_account__workspace=workspace).values_list(
            "zernio_account_id", flat=True
        )
    )
    for account in accounts:
        account["already_connected"] = account["account_id"] in bound_ids
        account["platform_label"] = DESTINATION_LABELS.get(
            account.get("platform") or "", account.get("platform") or ""
        )
    return render(
        request,
        "zernio/discover.html",
        {
            "workspace": workspace,
            "workspace_id": workspace.id,
            "key": key,
            "accounts": accounts,
        },
    )


@login_required
@require_POST
@ratelimit(key="user", rate="10/m", method="POST", block=True)
def connect_account(request, workspace_id):
    """Create the SocialAccount + binding for one discovered account."""
    workspace = _get_workspace(request, workspace_id)
    key = _get_key(request, workspace, request.POST.get("key_id"))
    account = {
        "account_id": (request.POST.get("account_id") or "").strip(),
        "platform": (request.POST.get("platform") or "").strip().lower(),
        "username": (request.POST.get("username") or "").strip(),
        "display_name": (request.POST.get("display_name") or "").strip(),
        "profile_id": (request.POST.get("profile_id") or "").strip(),
    }
    if not account["account_id"] or not account["platform"]:
        messages.error(request, "Missing account details. Try discovering again.")
        return redirect("zernio:discover", workspace_id=workspace.id, key_id=key.id)
    try:
        social_account, _binding = services.connect_account(workspace, api_key=key, account=account)
    except services.AlreadyBoundError as exc:
        messages.error(request, str(exc))
    except services.NoFreeKeyError as exc:
        messages.error(request, str(exc))
    except ProviderError as exc:
        messages.error(request, f"Could not connect the account: {exc}")
    else:
        messages.success(
            request,
            f"Connected {social_account.account_name} via Zernio. It can now be used for posts like any other account.",
        )
    return redirect("zernio:dashboard", workspace_id=workspace.id)


# ----------------------------------------------------------------------
# Bindings
# ----------------------------------------------------------------------


@login_required
@require_POST
def unbind_account(request, workspace_id, binding_id):
    """Drop a binding and disconnect the account. The key is untouched."""
    workspace = _get_workspace(request, workspace_id)
    binding = get_object_or_404(
        ZernioAccountBinding, id=binding_id, social_account__workspace=workspace
    )
    account = services.unbind_account(binding)
    messages.warning(
        request,
        f"{account.account_name} is unbound and disconnected. Rebind it from the Zernio page to publish again.",
    )
    return redirect("zernio:dashboard", workspace_id=workspace.id)


@login_required
@require_POST
def rebind_account(request, workspace_id, binding_id):
    """Move a binding to another key with a free slot."""
    workspace = _get_workspace(request, workspace_id)
    binding = get_object_or_404(
        ZernioAccountBinding, id=binding_id, social_account__workspace=workspace
    )
    new_key = _get_key(request, workspace, request.POST.get("key_id"))
    try:
        services.rebind_account(binding, new_key)
    except services.NoFreeKeyError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            f"{binding.social_account.account_name} is now bound to key {new_key.display_name}.",
        )
    return redirect("zernio:dashboard", workspace_id=workspace.id)
