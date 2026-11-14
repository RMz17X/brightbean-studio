"""Zernio aggregator provider: publish via zernio.com/api/v1 with a pooled API key.

Zernio is a scheduling/publishing API, not an OAuth platform: one ``sk_``
key reaches every social account connected under that Zernio account, and
one ``POST /v1/posts`` can publish to several platforms at once. BrightBean
models each Zernio-connected destination account as its own
``SocialAccount`` with ``platform="zernio"``; the destination platform,
the Zernio account id and the profile id travel in the provider's
``credentials`` dict (injected by the publish engine from the account's
key binding — see ``apps.publisher.engine._resolve_publish_credentials``),
and the API key itself lives in the ``apps.zernio`` key pool, never in the
account row.

Flow (mirrors the working ``upload-zernio-curl.sh``):

1. ``POST /v1/media/presign`` ``{filename, contentType, size}`` — only
   when the media has no public URL yet; the engine normally hands us a
   public/presigned URL already (``needs_local_media`` is False).
2. ``PUT <uploadUrl>`` with the raw bytes (httpx — never urllib: the old
   urllib presign path died with "Remote end closed connection").
3. ``POST /v1/posts`` ``{content, title, visibility, profileId,
   platforms: [{platform, accountId}], mediaItems, ...}`` with
   ``publishNow`` (the engine only calls us when the post is due).
4. ``GET /v1/posts/{id}`` to settle the async publish — Zernio reports
   ``scheduled`` → ``publishing`` → ``published``.

Transport map — routing follows the *account*, never the destination
platform; two methods exist side by side:
- YouTube: direct (``YouTubeProvider``, OAuth) OR via Zernio, depending
  on how the account is connected. A YouTube account bound through
  Zernio publishes through Zernio; nothing in this provider refuses a
  YouTube destination. A disconnected/erroring Zernio YouTube account is
  an account-connection problem — fixed by reconnecting it at
  zernio.com — not a reason to refuse the route.
- Facebook / Instagram / TikTok: via Zernio only. Their direct
  providers exist in code but are dormant without a Meta/TikTok app, so
  every connected FB/IG/TT account is a ``platform="zernio"`` account
  and always lands on this provider (see
  ``apps.publisher.engine._dispatch_to_provider``).

Discovery: ``GET /v1/profiles`` (pick the default profile) then
``GET /v1/accounts?profileId=<id>`` — the same two calls the local
``upload-zernio.py --discover`` makes.
"""

from __future__ import annotations

import logging
import mimetypes
import os
from dataclasses import dataclass

import httpx

from .base import SocialProvider
from .exceptions import APIError, ProviderError, PublishError
from .types import (
    AccountProfile,
    AuthType,
    MediaType,
    PostType,
    PublishContent,
    PublishResult,
    PublishState,
    PublishStatus,
    RateLimitConfig,
)

logger = logging.getLogger(__name__)

API_BASE = "https://zernio.com/api/v1"

# Zernio's free tier: 2 connected accounts per API key. The key pool in
# ``apps.zernio`` enforces this; the provider only needs the number for
# its own documentation.
MAX_ACCOUNTS_PER_KEY = 2

# BrightBean destination platform -> Zernio platform slug in
# ``POST /v1/posts`` ``platforms`` entries. Only youtube/tiktok are proven
# end-to-end by the local upload scripts; the rest follow Zernio's own
# platform list (llms.txt) and are validated by the discovery call, which
# returns the slug Zernio itself uses for each connected account.
# "youtube" stays a supported Zernio destination: routing follows the
# account (see the module docstring), so a Zernio-bound YouTube account
# publishes here while a direct YouTube account never reaches this
# provider.
DESTINATION_PLATFORMS: dict[str, str] = {
    "youtube": "youtube",
    "tiktok": "tiktok",
    "facebook": "facebook",
    "instagram": "instagram",
    "threads": "threads",
    "pinterest": "pinterest",
    "bluesky": "bluesky",
    "google_business": "google_business",
    "linkedin_company": "linkedin",
    "linkedin_personal": "linkedin",
    # Zernio's own slug (as returned by discovery) maps to itself, so a
    # binding created from discovery works without translation.
    "linkedin": "linkedin",
}

# Per-destination caption ceiling, so the composer counts against the
# platform the post will actually land on. Unknown destinations get the
# TikTok/Instagram ceiling (2,200), the strictest common one.
CAPTION_LIMITS: dict[str, int] = {
    "youtube": 5000,
    "tiktok": 2200,
    "instagram": 2200,
    "facebook": 63206,
    "threads": 500,
    "pinterest": 500,
    "bluesky": 300,
    "google_business": 1500,
    "linkedin": 3000,
}

# TikTok settings copied from the working local upload script. AI-made
# footage is disclosed through ``video_made_with_ai`` when the post's
# platform_extra sets it; the default (False) matches current behaviour.
TIKTOK_SETTINGS: dict = {
    "privacy_level": "PUBLIC_TO_EVERYONE",
    "allow_comment": True,
    "allow_duet": True,
    "allow_stitch": True,
    "commercial_content_type": "none",
    "content_preview_confirmed": True,
    "express_consent_given": True,
    "media_type": "video",
    "auto_add_music": False,
    "video_made_with_ai": False,
}

# TikTok privacy levels the composer's panel can submit (mirrors
# providers.tiktok.VALID_PRIVACY_LEVELS; kept local so this module does
# not import the TikTok provider).
TIKTOK_PRIVACY_LEVELS = frozenset(
    {
        "PUBLIC_TO_EVERYONE",
        "MUTUAL_FOLLOW_FRIENDS",
        "FOLLOWER_OF_CREATOR",
        "SELF_ONLY",
    }
)

# YouTube privacy_status -> Zernio visibility. Zernio accepts only
# "public"/"private" on POST /v1/posts: there is no "unlisted".
# Mapping decision (documented, lossy by design): "unlisted" -> "private".
# Rationale: unlisted and private both hide the video from the public
# listing/search; private is the strictly safer side — a video the author
# meant to restrict must never become public because the transport lacks
# the middle setting. Impact: a link that would have worked for anyone
# with the URL stops working; the author must flip the video to public
# (or unlisted, on YouTube directly) to share it. The composer shows
# this in the YouTube panel, so the downgrade is never silent.
UNLISTED_VISIBILITY_FALLBACK = "private"


def resolve_visibility(extra: dict | None) -> str:
    """Map composer visibility to a Zernio ``visibility`` value.

    Prefers an explicit ``visibility`` key, else the YouTube
    ``privacy_status`` the composer stores; anything else (including
    ``unlisted``) falls back as documented above. Unknown or missing
    values fail CLOSED to ``"private"``: a video the author meant to
    restrict must never become public because the transport could not
    read the setting — the same principle as ``unlisted`` -> ``private``.
    """
    raw = (extra or {}).get("visibility") or (extra or {}).get("privacy_status") or "private"
    raw = str(raw).strip().lower()
    if raw in ("public", "private"):
        return raw
    if raw == "unlisted":
        logger.info("Zernio has no 'unlisted' visibility; publishing as 'private' instead.")
        return UNLISTED_VISIBILITY_FALLBACK
    logger.info("Unknown visibility %r; publishing as 'private' (fail-closed).", raw)
    return "private"


# Zernio post status -> BrightBean publish state.
_POST_STATUS_MAP: dict[str, PublishState] = {
    "published": PublishState.COMPLETE,
    "failed": PublishState.FAILED,
    "rejected": PublishState.FAILED,
    "error": PublishState.FAILED,
    "deleted": PublishState.FAILED,
    "scheduled": PublishState.PENDING,
    "publishing": PublishState.PENDING,
    "processing": PublishState.PENDING,
    "queued": PublishState.PENDING,
    "draft": PublishState.PENDING,
}


def mask_api_key(key: str | None) -> str:
    """Last-4 masking for display/logs. Never returns the full key."""
    if not key:
        return "****"
    key = str(key)
    return "****" + key[-4:] if len(key) > 4 else "****"


def parse_profiles_response(data: dict) -> list[dict]:
    """Normalise ``GET /v1/profiles`` into ``[{id, name, is_default}]``."""
    profiles = []
    for item in data.get("profiles", []) or []:
        if not isinstance(item, dict) or not item.get("_id"):
            continue
        profiles.append(
            {
                "id": item["_id"],
                "name": item.get("name") or "",
                "is_default": bool(item.get("isDefault")),
            }
        )
    return profiles


def pick_default_profile(profiles: list[dict]) -> dict | None:
    """The default profile, else the first one, else None."""
    if not profiles:
        return None
    return next((p for p in profiles if p.get("is_default")), profiles[0])


def parse_accounts_response(data: dict, profile_id: str = "") -> list[dict]:
    """Normalise ``GET /v1/accounts`` into account dicts.

    Each item: ``account_id`` (Zernio ``_id``), ``platform`` (lowercased
    slug), ``username``, ``display_name``, ``is_active``, ``profile_id``.
    """
    accounts = []
    for item in data.get("accounts", []) or []:
        if not isinstance(item, dict) or not item.get("_id"):
            continue
        platform = str(item.get("platform") or "").lower()
        accounts.append(
            {
                "account_id": item["_id"],
                "platform": platform,
                "username": item.get("username") or "",
                "display_name": item.get("displayName") or item.get("username") or "",
                "is_active": bool(item.get("isActive", True)),
                "profile_id": profile_id,
            }
        )
    return accounts


def map_post_status(status: str | None) -> PublishState:
    """Zernio's status string -> :class:`PublishState` (unknown = pending)."""
    return _POST_STATUS_MAP.get(str(status or "").lower(), PublishState.PENDING)


# Keys that can carry a post id in a Zernio response body. Zernio wraps
# single-post payloads (``{"post": {...}}`` — proven live 2026-11-14: the
# GET /v1/posts/{id} body carries status inside ``post``), and its
# duplicate error names the post that already exists via
# ``existingPostId``. The create response has been seen with the id in
# none of the top-level fields the old parser read — which is exactly how
# a live YouTube publish was recorded as a failure and retried into 409
# duplicates.
_POST_ID_KEYS = ("_id", "id", "postId", "post_id", "existingPostId", "existing_post_id")


def _post_response_candidates(data: dict) -> list[dict]:
    """Response body first, then its ``post``/``data`` wrappers, in order."""
    candidates = [data]
    for key in ("post", "data"):
        nested = data.get(key)
        if isinstance(nested, dict):
            candidates.append(nested)
    return candidates


def extract_zernio_post_id(data: dict) -> str:
    """Post id from a Zernio response body, in every shape Zernio uses.

    Searches the top level first (the old behaviour), then the ``post``
    and ``data`` wrappers. Returns "" when no id can be found anywhere.
    """
    for candidate in _post_response_candidates(data):
        for id_key in _POST_ID_KEYS:
            value = candidate.get(id_key)
            if value:
                return str(value)
    return ""


def extract_zernio_field(data: dict, field: str) -> str:
    """A scalar field (``status``/``url``/``error``/...) from a Zernio body.

    Same search order as :func:`extract_zernio_post_id`: top level,
    then ``post``, then ``data``. Returns "" when the field is absent.
    """
    for candidate in _post_response_candidates(data):
        value = candidate.get(field)
        if value:
            return str(value)
    return ""


def build_post_payload(
    *,
    content: PublishContent,
    profile_id: str,
    destination_platform: str,
    account_id: str,
) -> dict:
    """Build the ``POST /v1/posts`` body for one destination account.

    Pure function (no network) so tests can pin the exact payload the
    working local scripts send: ``content``/``title``/``visibility``/
    ``profileId``/``platforms``/``mediaItems`` plus the TikTok settings
    block for TikTok destinations. Draft/schedule is the read side of
    the composer contract — the composer writes ``platform_extra`` (see
    ``apps.composer.services``): ``is_draft`` (bool) -> ``isDraft``;
    ``scheduled_for`` (ISO-8601 string) -> ``scheduledFor``. A draft wins
    when both are set; with neither key present this sets ``publishNow``
    and sends neither field, so old posts behave exactly as before.

    Option mapping (composer panel -> Zernio): YouTube ``privacy_status``
    becomes ``visibility`` via :func:`resolve_visibility` (``unlisted``
    -> ``private``, lossy — see the constant above); ``tags`` pass
    through; TikTok ``privacy_level`` / ``disable_comment`` /
    ``disable_duet`` / ``disable_stitch`` / ``is_aigc`` become the
    ``tiktokSettings`` block. Options with no Zernio field are
    deliberately NOT sent: YouTube thumbnail / made_for_kids, TikTok
    brand-disclosure toggles / cover timestamp, Facebook Reel hint,
    Pinterest board / link / alt text / cover.
    """
    zernio_platform = DESTINATION_PLATFORMS.get(destination_platform)
    if zernio_platform is None:
        raise PublishError(
            f"Zernio cannot publish to destination {destination_platform!r}.",
            platform="Zernio",
            retryable=False,
        )
    title = (content.title or "").strip() or (content.text or "").strip()[:100] or "Untitled"
    title = title[:100]  # YouTube's title ceiling, as in the local scripts.
    text = content.text or ""
    extra = content.extra or {}

    media_items: list[dict] = []
    for index, url in enumerate(content.media_urls or []):
        media_items.append(
            {"type": "video" if content.is_video(index) else "image", "url": url}
        )

    payload: dict = {
        "content": text or title,
        "title": title,
        "visibility": resolve_visibility(extra),
        "profileId": profile_id,
        "platforms": [{"platform": zernio_platform, "accountId": account_id}],
        "mediaItems": media_items,
    }
    tags = extra.get("tags")
    if tags:
        payload["tags"] = [str(t).strip() for t in tags if str(t).strip()]
    if extra.get("is_draft"):
        payload["isDraft"] = True
    elif extra.get("scheduled_for"):
        payload["scheduledFor"] = extra["scheduled_for"]
    else:
        payload["publishNow"] = True
    if zernio_platform == "tiktok":
        settings = dict(TIKTOK_SETTINGS)
        # Composer TikTok panel -> tiktokSettings. privacy_level is
        # validated against TikTok's own levels (an unknown value keeps
        # the default rather than failing the whole post); the allow_*
        # toggles are the inverse of the composer's disable_* flags.
        privacy_level = extra.get("privacy_level")
        if privacy_level in TIKTOK_PRIVACY_LEVELS:
            settings["privacy_level"] = privacy_level
        settings["allow_comment"] = not bool(extra.get("disable_comment", False))
        settings["allow_duet"] = not bool(extra.get("disable_duet", False))
        settings["allow_stitch"] = not bool(extra.get("disable_stitch", False))
        if "video_made_with_ai" in extra:
            settings["video_made_with_ai"] = bool(extra["video_made_with_ai"])
        elif extra.get("is_aigc") is not None:
            settings["video_made_with_ai"] = bool(extra["is_aigc"])
        payload["tiktokSettings"] = settings
    return payload


@dataclass
class PresignedUpload:
    upload_url: str
    public_url: str


class ZernioProvider(SocialProvider):
    """Publish to YouTube/TikTok/Facebook/Instagram/... via Zernio.

    The provider is constructed with app credentials from
    ``resolve_platform_credentials`` — Zernio has no app-level credentials,
    so the publish engine injects the per-account values instead (see
    ``_resolve_publish_credentials``): ``api_key`` (from the key pool),
    ``account_id`` (Zernio account ``_id``), ``destination_platform``
    (BrightBean platform slug) and ``profile_id``. Direct callers may pass
    the API key as the ``access_token`` argument instead.
    """

    # Zernio takes a public URL and fetches the media itself, so the engine
    # must not download bytes to local disk for us — ``media_urls`` is all
    # we read. (``_dispatch_to_provider`` still collects the URLs.)
    needs_local_media = False

    # Zernio accepts the post and processes it asynchronously; the engine
    # holds the row in ``publishing`` and settles it from
    # :meth:`check_publish_status`.
    publish_is_async = True

    def __init__(self, credentials: dict | None = None):
        super().__init__(credentials)

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @property
    def platform_name(self) -> str:
        return "Zernio"

    @property
    def auth_type(self) -> AuthType:
        return AuthType.API_KEY

    @property
    def max_caption_length(self) -> int:
        destination = str(self.credentials.get("destination_platform") or "")
        slug = DESTINATION_PLATFORMS.get(destination, "")
        return CAPTION_LIMITS.get(slug, CAPTION_LIMITS.get(destination, 2200))

    @property
    def supported_post_types(self) -> list[PostType]:
        return [PostType.TEXT, PostType.IMAGE, PostType.VIDEO, PostType.SHORT, PostType.REEL]

    @property
    def supported_media_types(self) -> list[MediaType]:
        return [MediaType.JPEG, MediaType.PNG, MediaType.GIF, MediaType.WEBP, MediaType.MP4, MediaType.MOV]

    @property
    def required_scopes(self) -> list[str]:
        return []  # API-key auth, no OAuth scopes.

    @property
    def rate_limits(self) -> RateLimitConfig:
        # Free tier: 60 requests/minute.
        return RateLimitConfig(requests_per_hour=3600, requests_per_day=5000, publish_per_day=25)

    # ------------------------------------------------------------------
    # Credential resolution
    # ------------------------------------------------------------------

    def _api_key(self, access_token: str | None = None) -> str:
        """The Zernio API key for this call.

        Engine path: injected in ``credentials`` from the account's key
        binding. Direct path: passed as ``access_token``. A 401 from Zernio
        is permanent (bad/revoked key), so callers must not retry it.
        """
        key = (self.credentials.get("api_key") or "") or (access_token or "")
        if not key or not str(key).startswith("sk_"):
            raise PublishError(
                "Zernio account is not bound to an API key. Bind it to a key on the Zernio page first.",
                platform=self.platform_name,
                retryable=False,
            )
        return str(key)

    def _account_id(self) -> str:
        account_id = str(self.credentials.get("account_id") or "")
        if not account_id:
            raise PublishError(
                "Zernio account has no Zernio account id. Reconnect it from the Zernio page.",
                platform=self.platform_name,
                retryable=False,
            )
        return account_id

    def _profile_id(self) -> str:
        profile_id = str(self.credentials.get("profile_id") or "")
        if not profile_id:
            raise PublishError(
                "Zernio account has no Zernio profile id. Reconnect it from the Zernio page.",
                platform=self.platform_name,
                retryable=False,
            )
        return profile_id

    def _destination_platform(self) -> str:
        destination = str(self.credentials.get("destination_platform") or "")
        if destination not in DESTINATION_PLATFORMS:
            raise PublishError(
                f"Zernio cannot publish to destination {destination!r}.",
                platform=self.platform_name,
                retryable=False,
            )
        return destination

    # ------------------------------------------------------------------
    # Discovery (profiles + accounts)
    # ------------------------------------------------------------------

    def list_profiles(self, api_key: str | None = None) -> list[dict]:
        """``GET /v1/profiles`` -> normalised profile list."""
        key = self._api_key(api_key)
        try:
            response = self._request("GET", f"{API_BASE}/profiles", access_token=key)
        except APIError as exc:
            if exc.status_code in (401, 403):
                raise self._auth_error_from(exc)
            raise
        return parse_profiles_response(self._safe_json(response))

    def _auth_error_from(self, exc: APIError) -> APIError:
        return APIError(
            "Zernio rejected the API key (HTTP %s). Check the key on the Zernio page — it may have been revoked."
            % exc.status_code,
            status_code=exc.status_code,
            platform=self.platform_name,
            raw_response=exc.raw_response,
            retryable=False,
        )

    def list_accounts(self, api_key: str | None = None, profile_id: str | None = None) -> list[dict]:
        """``GET /v1/accounts?profileId=...`` -> normalised account list.

        With no ``profile_id`` the default profile (else the first) is used,
        mirroring ``upload-zernio.py --discover``.
        """
        key = self._api_key(api_key)
        if not profile_id:
            profiles = self.list_profiles(key)
            profile = pick_default_profile(profiles)
            if profile is None:
                raise ProviderError(
                    "This Zernio key has no profiles. Create a profile and connect accounts at zernio.com first.",
                    platform=self.platform_name,
                    retryable=False,
                )
            profile_id = profile["id"]
        try:
            response = self._request(
                "GET", f"{API_BASE}/accounts", access_token=key, params={"profileId": profile_id}
            )
        except APIError as exc:
            if exc.status_code in (401, 403):
                raise self._auth_error_from(exc)
            raise
        return parse_accounts_response(self._safe_json(response), profile_id)

    def validate_token(self, access_token: str | None = None) -> bool:
        """True when Zernio accepts the key (a profiles call succeeds)."""
        try:
            self.list_profiles(access_token)
        except ProviderError:
            return False
        return True

    # ------------------------------------------------------------------
    # Media upload (presign + PUT) — only when no public URL exists
    # ------------------------------------------------------------------

    def presign_upload(self, file_path: str, api_key: str | None = None) -> PresignedUpload:
        """``POST /v1/media/presign`` -> upload + public URLs.

        Request body mirrors the working curl script exactly:
        ``{filename, contentType, size}``. The PUT itself uses httpx, never
        urllib (the old urllib presign path died mid-request).
        """
        key = self._api_key(api_key)
        size = os.path.getsize(file_path)
        filename = os.path.basename(file_path)
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        try:
            response = self._request(
                "POST",
                f"{API_BASE}/media/presign",
                access_token=key,
                json={"filename": filename, "contentType": content_type, "size": size},
            )
        except APIError as exc:
            if exc.status_code in (401, 403):
                raise self._auth_error_from(exc)
            raise
        data = self._safe_json(response)
        if not data.get("uploadUrl") or not data.get("publicUrl"):
            raise ProviderError(
                "Zernio presign did not return uploadUrl/publicUrl.",
                platform=self.platform_name,
            )
        return PresignedUpload(upload_url=data["uploadUrl"], public_url=data["publicUrl"])

    def upload_file(self, file_path: str, api_key: str | None = None) -> str:
        """Presign + PUT a local file, returning its public URL."""
        key = self._api_key(api_key)
        presigned = self.presign_upload(file_path, key)
        content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        with open(file_path, "rb") as handle:
            with httpx.Client(timeout=600) as client:
                response = client.put(
                    presigned.upload_url,
                    content=handle,
                    headers={"Content-Type": content_type},
                )
        if response.status_code >= 400:
            raise self._error_for_response(response)
        return presigned.public_url

    # ------------------------------------------------------------------
    # Profile (abstract) + publishing
    # ------------------------------------------------------------------

    def get_profile(self, access_token: str | None = None) -> AccountProfile:
        """The bound Zernio account, looked up in the key's account list."""
        key = self._api_key(access_token)
        account_id = self.credentials.get("account_id") or ""
        profile_id = self.credentials.get("profile_id") or ""
        accounts = self.list_accounts(key, profile_id or None)
        match = next((a for a in accounts if a["account_id"] == account_id), None)
        if match is None:
            raise ProviderError(
                "Zernio account not found under this API key. It may have been disconnected at zernio.com.",
                platform=self.platform_name,
                retryable=False,
            )
        return AccountProfile(
            platform_id=match["account_id"],
            name=match["display_name"] or match["username"],
            handle=match["username"] or None,
            extra={
                "platform": match["platform"],
                "profile_id": match["profile_id"],
                "is_active": match["is_active"],
            },
        )

    def publish_post(self, access_token: str | None = None, content: PublishContent | None = None) -> PublishResult:
        """``POST /v1/posts`` for the bound destination account.

        YouTube is a supported destination here when the account is
        Zernio-bound — routing follows the account, and nothing in this
        method refuses a YouTube destination. A disconnected/erroring
        Zernio YouTube account fails earlier, at credential/profile
        lookup, which is an account-connection problem, not a refusal
        of the route.

        Reconciliation, not blind retry: a 2xx whose body carries no
        post id in any shape is a non-retryable failure (the post may
        already be live — retrying would only 409), and a 409 duplicate
        reconciles to the existing post id instead of failing.
        """
        if content is None:
            raise PublishError("Nothing to publish.", platform=self.platform_name, retryable=False)
        key = self._api_key(access_token)
        destination = self._destination_platform()
        payload = build_post_payload(
            content=content,
            profile_id=self._profile_id(),
            destination_platform=destination,
            account_id=self._account_id(),
        )
        try:
            response = self._request("POST", f"{API_BASE}/posts", access_token=key, json=payload)
        except APIError as exc:
            if exc.status_code in (401, 403):
                raise self._auth_error_from(exc)
            if exc.status_code == 409:
                # Duplicate: Zernio refused a second copy of content it
                # already accepted. The body names the post that already
                # exists — reconcile to it instead of failing or retrying
                # (a retry would 409 again and never create a new post).
                existing_id = extract_zernio_post_id(exc.raw_response or {})
                if existing_id:
                    logger.warning(
                        "Zernio 409 duplicate for %s; reconciling to existing post %s",
                        destination,
                        existing_id,
                    )
                    return PublishResult(
                        platform_post_id=existing_id,
                        url=None,
                        extra={
                            "zernio_status": "duplicate",
                            "destination_platform": destination,
                            "reconciled_from_409": True,
                        },
                    )
                raise ProviderError(
                    "Zernio reports this content was already posted (duplicate) but named no existing post id. Check zernio.com before republishing.",
                    platform=self.platform_name,
                    raw_response=exc.raw_response or {},
                    retryable=False,
                )
            raise
        data = self._safe_json(response)
        post_id = extract_zernio_post_id(data)
        if not post_id:
            # The POST itself succeeded (2xx), so the post may already be
            # live at Zernio — but with no id there is nothing to confirm
            # it against. NEVER retry this blindly: a retry re-sends the
            # same content and Zernio answers 409 duplicate. Fail
            # permanently with a message that says the outcome is unknown
            # and where to check, instead of scheduling a duplicate.
            raise ProviderError(
                "Zernio accepted the post but returned no post id. The post may already be live — check zernio.com before republishing; this will not be retried automatically.",
                platform=self.platform_name,
                raw_response=data,
                retryable=False,
            )
        status = extract_zernio_field(data, "status")
        url = extract_zernio_field(data, "url") or extract_zernio_field(data, "postUrl")
        logger.info("Zernio post %s created for %s (status=%s)", post_id, destination, status or "unknown")
        return PublishResult(
            platform_post_id=post_id,
            url=url,
            extra={"zernio_status": status, "destination_platform": destination},
        )

    def check_publish_status(self, access_token: str | None = None, handle: str = "") -> PublishStatus:
        """``GET /v1/posts/{id}`` -> mapped publish state.

        Zernio wraps the post in a ``post`` object — status/error/url
        are read from ``post.*`` with a top-level fallback.
        """
        if not handle:
            raise ProviderError("No Zernio post id to check.", platform=self.platform_name, retryable=False)
        key = self._api_key(access_token)
        try:
            response = self._request("GET", f"{API_BASE}/posts/{handle}", access_token=key)
        except APIError as exc:
            if exc.status_code in (401, 403):
                raise self._auth_error_from(exc)
            raise
        data = self._safe_json(response)
        status = extract_zernio_field(data, "status")
        state = map_post_status(status)
        error = ""
        if state is PublishState.FAILED:
            error = (
                extract_zernio_field(data, "error")
                or extract_zernio_field(data, "message")
                or f"Zernio reported status {status!r}."
            )
        url = extract_zernio_field(data, "url") or extract_zernio_field(data, "postUrl")
        return PublishStatus(
            state=state,
            platform_post_id=handle if state is PublishState.COMPLETE else "",
            error=error,
            raw={"status": status, "url": url} if url else {"status": status},
        )

    # ------------------------------------------------------------------
    # OAuth stubs (not applicable for API-key auth)
    # ------------------------------------------------------------------

    def get_auth_url(self, redirect_uri: str, state: str, code_verifier: str | None = None) -> str:
        raise NotImplementedError("Zernio uses API-key auth, not OAuth. Add a key on the Zernio page instead.")

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None = None):  # type: ignore[override]
        raise NotImplementedError("Zernio uses API-key auth, not OAuth. Add a key on the Zernio page instead.")
