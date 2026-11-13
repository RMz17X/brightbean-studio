"""Update a connected account's public profile (bio / Page info).

Shared by the Agent API router (``apps.api.routers.accounts``) and the
web account card (``apps.social_accounts.views``), so the two surfaces
cannot drift: one platform gate, one validation, one provider call,
one error mapping.

Platform rules (by *destination* platform — ``display_platform``,
never the raw ``platform``; Zernio is a transport):
- YouTube direct: channel description only, max 1000 characters. A
  via-Zernio YouTube account has no way to update its description —
  Zernio exposes no such endpoint — so it is refused with a message
  that says to connect YouTube directly.
- Facebook Page direct: about / description / website / phone via
  ``FacebookProvider.update_page_info``. No name field — the Graph API
  cannot rename a Page. Via-Zernio Facebook is refused the same way.
- TikTok / Instagram (direct or via Zernio): no update endpoint
  exists anywhere, so both are refused with the "change it manually
  in the app" message.
- Everything else: refused as unsupported.

Nothing here knows about HTTP — callers map ``ProfileServiceError``
onto their own error shape.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from urllib.parse import urlsplit

from .live_service import is_insufficient_scope
from .models import SocialAccount
from .provider_factory import _get_provider_for_platform, apply_analytics_scope_flag

logger = logging.getLogger(__name__)

YOUTUBE_DESCRIPTION_MAX_LENGTH = 1000

MANUAL_BIO_MESSAGE = "Bio hanya bisa diubah manual di aplikasi TikTok/Instagram."
YOUTUBE_VIA_ZERNIO_MESSAGE = (
    "Update deskripsi tidak tersedia via Zernio — sambungkan akun YouTube langsung."
)
FACEBOOK_VIA_ZERNIO_MESSAGE = (
    "Update info Page tidak tersedia via Zernio — sambungkan akun Facebook Page langsung."
)
YOUTUBE_SCOPE_MESSAGE = (
    "YouTube menolak update deskripsi: token akun ini tidak punya scope penuh. "
    "Butuh re-authorize dengan scope penuh — tombol Reconnect for Live di halaman Live."
)
FACEBOOK_SCOPE_MESSAGE = (
    "Facebook menolak update Page: token Page tidak punya izin pages_manage_metadata, "
    "atau akun ini tidak bisa mengelola Page tersebut. Reconnect akun Facebook lalu coba lagi."
)


class ProfileServiceError(Exception):
    """A profile update failed, with a caller-facing message.

    ``status_code`` is the HTTP status the REST router should answer
    with. ``insufficient_scope`` marks the failure the operator can fix
    themselves by reconnecting the account with a wider grant.
    """

    def __init__(self, message: str, *, status_code: int = 502, insufficient_scope: bool = False):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.insufficient_scope = insufficient_scope


def provider_and_token(account: SocialAccount, platform: str):
    """Build the provider for ``account`` with a usably-fresh token.

    Same best-effort refresh as the publish engine and the Live service:
    on refresh failure we keep the old token and let the API call
    surface the real error.
    """
    provider = _get_provider_for_platform(platform, account.workspace.organization_id)
    apply_analytics_scope_flag(provider, platform)
    token = account.oauth_access_token
    if account.token_expires_within(timedelta(days=1)) and account.oauth_refresh_token:
        try:
            token = account.refresh_oauth_token(provider)
        except Exception:
            logger.warning("Profile: token refresh failed for %s", account, exc_info=True)
    return provider, token


def _clean(value: str | None) -> str | None:
    """Strip a field; a blank string counts as "not provided"."""
    if value is None:
        return None
    value = value.strip()
    return value or None


def _validate_website(website: str) -> None:
    parts = urlsplit(website)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ProfileServiceError("website harus URL yang valid (http/https).", status_code=422)


def update_account_profile(
    account: SocialAccount,
    *,
    description: str | None = None,
    about: str | None = None,
    website: str | None = None,
    phone: str | None = None,
) -> dict:
    """Update ``account``'s public profile on its destination platform.

    Returns the updated fields as plain data (``platform``,
    ``transport``, the fields that were sent, ``updated_fields``).
    Raises :class:`ProfileServiceError` for every refusal — platform
    gate, validation, insufficient scope, provider failure.
    """
    if account.connection_status != SocialAccount.ConnectionStatus.CONNECTED:
        raise ProfileServiceError(
            "Akun ini tidak terhubung. Reconnect akun dulu, lalu coba lagi.",
            status_code=422,
        )

    # An explicitly-sent empty description means "clear it" — the
    # YouTube provider documents empty = clear — while a missing field
    # (None) still means "not provided" and is refused below. Only
    # description gets this treatment; the other fields keep _clean's
    # blank-means-omitted rule.
    description_provided = description is not None
    description = _clean(description)
    if description_provided and description is None:
        description = ""
    about = _clean(about)
    website = _clean(website)
    phone = _clean(phone)
    if website is not None:
        _validate_website(website)

    platform = account.display_platform

    if platform == "youtube":
        return _update_youtube(account, description=description)
    if platform == "facebook":
        return _update_facebook(
            account, about=about, description=description, website=website, phone=phone
        )
    if platform in ("tiktok", "instagram"):
        raise ProfileServiceError(MANUAL_BIO_MESSAGE, status_code=422)
    raise ProfileServiceError(
        f"Update profil tidak didukung untuk {account.display_platform_label}.",
        status_code=422,
    )


def _update_youtube(account: SocialAccount, *, description: str | None) -> dict:
    if account.is_via_zernio:
        raise ProfileServiceError(YOUTUBE_VIA_ZERNIO_MESSAGE, status_code=422)
    if description is None:
        raise ProfileServiceError(
            "description wajib diisi untuk akun YouTube.", status_code=422
        )
    if len(description) > YOUTUBE_DESCRIPTION_MAX_LENGTH:
        raise ProfileServiceError(
            f"Deskripsi YouTube maks {YOUTUBE_DESCRIPTION_MAX_LENGTH} karakter.",
            status_code=422,
        )
    provider, token = provider_and_token(account, "youtube")
    # ``update_channel_description`` is built by a parallel worker in
    # providers/youtube.py; the endpoint must not invent a second one.
    # Assumed signature (mirrors the provider's other methods):
    #   update_channel_description(access_token, *, description, channel_id=None) -> dict
    if not hasattr(provider, "update_channel_description"):
        raise ProfileServiceError(
            "Metode update deskripsi YouTube belum tersedia di provider ini.",
            status_code=502,
        )
    try:
        provider.update_channel_description(
            token,
            description=description,
            channel_id=account.account_platform_id,
        )
    except Exception as exc:
        if is_insufficient_scope(exc):
            raise ProfileServiceError(
                YOUTUBE_SCOPE_MESSAGE, status_code=403, insufficient_scope=True
            ) from exc
        raise ProfileServiceError(
            f"YouTube gagal memperbarui deskripsi: {exc}", status_code=502
        ) from exc
    return {
        "platform": "youtube",
        "transport": account.transport,
        "description": description,
        "updated_fields": ["description"],
    }


def _update_facebook(
    account: SocialAccount,
    *,
    about: str | None,
    description: str | None,
    website: str | None,
    phone: str | None,
) -> dict:
    if account.is_via_zernio:
        raise ProfileServiceError(FACEBOOK_VIA_ZERNIO_MESSAGE, status_code=422)
    given = {
        name: value
        for name, value in (
            ("about", about),
            ("description", description),
            ("website", website),
            ("phone", phone),
        )
        if value is not None
    }
    if not given:
        raise ProfileServiceError(
            "Isi minimal satu field: about, description, website, atau phone.",
            status_code=422,
        )
    provider, token = provider_and_token(account, "facebook")
    if not hasattr(provider, "update_page_info"):
        raise ProfileServiceError(
            "Metode update info Page belum tersedia di provider ini.",
            status_code=502,
        )
    try:
        result = provider.update_page_info(
            token,
            account.account_platform_id,
            about=about,
            description=description,
            website=website,
            phone=phone,
        )
    except Exception as exc:
        if getattr(exc, "status_code", None) == 403 or is_insufficient_scope(exc):
            raise ProfileServiceError(
                FACEBOOK_SCOPE_MESSAGE, status_code=403, insufficient_scope=True
            ) from exc
        raise ProfileServiceError(
            f"Facebook gagal memperbarui info Page: {exc}", status_code=502
        ) from exc
    updated_fields = (
        list(result.get("updated_fields") or [])
        if isinstance(result, dict)
        else sorted(given)
    ) or sorted(given)
    return {
        "platform": "facebook",
        "transport": account.transport,
        **given,
        "updated_fields": updated_fields,
    }
