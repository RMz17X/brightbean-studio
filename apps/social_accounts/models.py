import uuid
from typing import Any

from django.conf import settings
from django.db import models, router, transaction
from django.utils import timezone

from apps.common.encryption import EncryptedTextField
from apps.common.managers import WorkspaceScopedManager
from apps.credentials.models import PlatformCredential


class YouTubeRevocation(models.Model):
    """Encrypted, short-lived grant retry, independent of a deleted account."""

    token = EncryptedTextField()
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    next_attempt_at = models.DateTimeField(default=timezone.now)
    attempts = models.PositiveIntegerField(default=0)


class SocialAccount(models.Model):
    _skip_analytics_backfill: bool

    class AnalyticsStatus(models.TextChoices):
        UNKNOWN = "unknown", "Not checked"
        CHECKING = "checking", "Checking"
        READY = "ready", "Ready"
        RECONNECT = "reconnect_required", "Reconnect required"
        ATTENTION = "attention_required", "Operator attention required"
        TEMPORARY = "temporary_error", "Temporary failure"

    class ConnectionStatus(models.TextChoices):
        CONNECTED = "connected", "Connected"
        TOKEN_EXPIRING = "token_expiring", "Token Expiring"
        DISCONNECTED = "disconnected", "Disconnected"
        ERROR = "error", "Error"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="social_accounts",
    )
    platform = models.CharField(
        max_length=30,
        choices=PlatformCredential.Platform.choices,
    )
    account_platform_id = models.CharField(
        max_length=255,
        help_text="The account's native ID on the platform.",
    )
    account_name = models.CharField(max_length=255)
    account_handle = models.CharField(max_length=255, blank=True, default="")
    avatar_url = models.URLField(max_length=2000, blank=True, default="")
    follower_count = models.IntegerField(default=0)

    # Encrypted OAuth tokens
    oauth_access_token = EncryptedTextField(blank=True, default="")
    oauth_refresh_token = EncryptedTextField(blank=True, default="")
    token_expires_at = models.DateTimeField(blank=True, null=True)
    # Facebook-login Instagram insights need the user's credential. The
    # primary token remains the Page credential used by publishing and inbox.
    oauth_facebook_user_access_token = EncryptedTextField(blank=True, default="")
    facebook_user_token_expires_at = models.DateTimeField(blank=True, null=True)
    analytics_status = models.CharField(max_length=25, choices=AnalyticsStatus.choices, default=AnalyticsStatus.UNKNOWN)
    analytics_error_reason = models.CharField(max_length=40, blank=True, default="")
    analytics_checked_at = models.DateTimeField(blank=True, null=True)
    analytics_credentials_version = models.PositiveIntegerField(default=0)

    # Instance URL for Mastodon and Bluesky PDS
    instance_url = models.URLField(max_length=500, blank=True, default="")

    # Object we subscribed for webhook delivery, when it isn't this account
    # itself. For Facebook Login Instagram this holds the linked Page ID used
    # for DM polling, sending and message subscriptions. Comment subscriptions
    # still address the Instagram account itself.
    webhook_target_id = models.CharField(max_length=255, blank=True, default="")
    messaging_access = models.BooleanField(null=True, blank=True, default=None)

    # Whether the platform is currently pushing this account's activity to us.
    # Null means "not applicable" (the platform has no webhooks) or "not tried
    # yet". False means the inbox will miss comments and mentions, which is
    # invisible without saying so — publishing and analytics still work, so the
    # connection itself stays healthy and `last_error` (owned by the periodic
    # health check) must not be borrowed for it.
    webhooks_active = models.BooleanField(null=True, blank=True, default=None)
    webhook_error = models.CharField(max_length=500, blank=True, default="")
    # Set when the subscription failed for a reason a retry cannot fix: the
    # grant itself is missing what it needs. Splits the card's CTA between
    # "Try again" (re-run the call) and "Reconnect" (get a new grant), so the
    # warning never asks for something the UI doesn't offer.
    webhook_needs_reconnect = models.BooleanField(default=False)
    # The provider's own words about the last failure. `webhook_error` is
    # rewritten for a human and is all the card shows; operators running
    # `diagnose_facebook` need the error code and fbtrace_id this keeps.
    webhook_error_detail = models.TextField(blank=True, default="")
    # Consecutive failed attempts, so the periodic health check can stop
    # re-trying a subscription that will never succeed. Reset by a success, by
    # a reconnect, and by the user pressing "Try again".
    webhook_retry_count = models.PositiveSmallIntegerField(default=0)

    # Scopes we asked for that the grant came back without. Meta drops
    # unapproved or declined permissions silently rather than failing the
    # grant, so without this the account looks healthy and only breaks later at
    # publish or insights time with an opaque platform error. Empty means
    # "everything we asked for was granted", or that the platform gives us no
    # way to ask.
    missing_scopes = models.JSONField(default=list, blank=True)

    # Connection health
    connection_status = models.CharField(
        max_length=20,
        choices=ConnectionStatus.choices,
        default=ConnectionStatus.CONNECTED,
    )
    last_health_check_at = models.DateTimeField(blank=True, null=True)
    last_error = models.TextField(blank=True, default="")

    connected_at = models.DateTimeField(auto_now_add=True)
    youtube_authorized_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="authorized_youtube_accounts",
    )
    youtube_authorization_checked_at = models.DateTimeField(null=True, blank=True)
    youtube_profile_refreshed_at = models.DateTimeField(null=True, blank=True)
    youtube_verification_started_at = models.DateTimeField(default=timezone.now)
    youtube_data_generation = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Per-account override of the Agent-API platform daily-post quota
    # (see apps/api/limits.py::PLATFORM_DAILY_POST_LIMIT). Null = use the
    # platform default. Useful when one specific integration is on a higher
    # upstream tier (e.g. an X account on Pro vs the default Basic cap).
    daily_post_limit_override = models.PositiveIntegerField(blank=True, null=True)

    # Set by the analytics sync layer when the platform rejects an analytics
    # call as insufficient-scope. Surfaces a "Reconnect for analytics" CTA in
    # place of the metric region. Cleared on successful reconnect.
    analytics_needs_reconnect = models.BooleanField(default=False)

    # When the inbox last polled this account, and when it last walked the
    # account's full history. Both belong here rather than being derived from
    # InboxMessage.received_at, which answers a different question: an account
    # that was polled and had nothing new to say leaves no message behind, so
    # the derived value would say "never polled" and re-poll it every cycle.
    #
    # The poll stamp is what lets one 5-minute cycle serve platforms with very
    # different budgets — see settings.INBOX_PLATFORM_MIN_POLL_SECONDS. The
    # sweep stamp paces the deep walk that catches a reply on a thread too old
    # for the routine poll's lookback (providers.youtube.get_messages).
    inbox_last_polled_at = models.DateTimeField(blank=True, null=True)
    inbox_last_deep_sweep_at = models.DateTimeField(blank=True, null=True)
    inbox_initial_backfill_cursor = models.TextField(blank=True, default="")
    inbox_initial_backfill_started_at = models.DateTimeField(blank=True, null=True)
    inbox_deep_sweep_cursor = models.TextField(blank=True, default="")
    inbox_deep_sweep_started_at = models.DateTimeField(blank=True, null=True)
    # Resume bounded Facebook Login Instagram DM walks across worker restarts.
    # Contains only object IDs/cursors, never OAuth tokens or message bodies.
    instagram_dm_poll_state = models.JSONField(default=dict, blank=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "social_accounts_social_account"
        unique_together = [("workspace", "platform", "account_platform_id")]

    def __str__(self):
        return f"{self.account_name} ({self.display_platform_label})"

    def save(self, *args, **kwargs):
        """Invalidate Instagram checks atomically when either credential changes."""
        credential_fields = {"oauth_access_token", "oauth_facebook_user_access_token", "facebook_user_token_expires_at"}
        fields = kwargs.get("update_fields")
        self._analytics_credentials_changed = False
        if self.platform != "instagram" or (fields is not None and not credential_fields.intersection(fields)):
            return super().save(*args, **kwargs)
        using = kwargs.get("using") or self._state.db or "default"
        with transaction.atomic(using=using):
            previous = type(self).objects.using(using).select_for_update().filter(pk=self.pk).first()
            changed = previous is not None and any(
                getattr(previous, field) != getattr(self, field)
                for field in credential_fields
                if fields is None or field in fields
            )
            # OAuth may return the same token after a consent change. A fresh
            # connection still gets a new verification generation.
            rechecking = (
                fields is not None
                and {"analytics_status", "oauth_facebook_user_access_token"}.issubset(fields)
                and self.analytics_status == self.AnalyticsStatus.CHECKING
            )
            if changed or rechecking or (previous is None and self.oauth_access_token):
                self._analytics_credentials_changed = True
                self.__dict__.pop("_instagram_analytics_grant", None)
                self.__dict__.pop("_instagram_analytics_status_recorded", None)
                self.analytics_credentials_version = (previous.analytics_credentials_version if previous else 0) + 1
                self.analytics_status = self.AnalyticsStatus.CHECKING
                self.analytics_error_reason = ""
                self.analytics_checked_at = None
                self.analytics_needs_reconnect = False
                if fields is not None:
                    kwargs["update_fields"] = set(fields) | {
                        "analytics_credentials_version",
                        "analytics_status",
                        "analytics_error_reason",
                        "analytics_checked_at",
                        "analytics_needs_reconnect",
                    }
            return super().save(*args, **kwargs)

    @property
    def instagram_dm_permission_request_disabled(self):
        return (
            self.platform == "instagram"
            and "instagram_manage_messages" in self.missing_scopes
            and not settings.PLATFORM_INSTAGRAM_FACEBOOK_DM_SCOPES_ENABLED
        )

    @property
    def connection_missing_scopes(self):
        # Instagram analytics has its own grant-aware status. A Page token's
        # scope list cannot establish whether the person declined insights.
        return [
            scope
            for scope in self.missing_scopes
            if self.platform != "instagram" or scope != "instagram_manage_insights"
            if scope != "instagram_manage_messages" or not self.instagram_dm_permission_request_disabled
        ]

    @property
    def webhook_warning(self):
        """The subscription warning and action applicable to current OAuth scopes."""
        from .error_messages import WebhookFailure, omit_disabled_instagram_dm_failure

        if self.webhooks_active is not False:
            return None
        failure = WebhookFailure(self.webhook_error, self.webhook_needs_reconnect)
        if self.instagram_dm_permission_request_disabled and "pages_manage_metadata" not in self.missing_scopes:
            return omit_disabled_instagram_dm_failure(failure)
        return failure

    @property
    def instagram_analytics_message(self):
        if self.platform != "instagram":
            return ""
        from apps.analytics.instagram_access import status_message

        return status_message(self)

    @property
    def display_label(self) -> str:
        """Human name for pickers and filters: the account name, else the handle.

        Mirrors the ``account_name|default:account_handle`` the channel filters
        used inline, so a component that can only read one attribute (the
        ``ui_select`` tag's ``label_field``) still renders the same text.
        """
        return self.account_name or self.account_handle

    @property
    def is_via_zernio(self) -> bool:
        """True when this account publishes through the Zernio transport."""
        return self.platform == "zernio"

    @property
    def display_platform(self) -> str:
        """Effective platform this account publishes to.

        Zernio is a transport, not a platform: a Zernio-bound account
        publishes to its binding's destination (YouTube, TikTok, ...),
        and every platform rule — caption limits, media requirements,
        field config — must see that destination. Routing still uses the
        raw ``platform`` ("zernio"); see the publish engine. An unbound
        Zernio row falls back to "zernio" so nothing crashes mid-flight.
        """
        if self.platform != "zernio":
            return self.platform
        try:
            binding = getattr(self, "zernio_binding", None)
        except (AttributeError, ValueError):
            return self.platform
        if binding is None:
            return self.platform
        return binding.destination_platform or self.platform

    @property
    def transport(self) -> str | None:
        """Transport carrying this account's publishes ("zernio"), else None."""
        return "zernio" if self.is_via_zernio else None

    @property
    def display_platform_label(self) -> str:
        """Human label for :attr:`display_platform` ("TikTok", "YouTube")."""
        if self.is_via_zernio:
            from apps.zernio.models import DESTINATION_LABELS

            return DESTINATION_LABELS.get(self.display_platform, self.display_platform)
        return self.get_platform_display()

    def token_expires_within(self, window) -> bool:
        """Whether the recorded expiry falls inside ``window`` from now.

        An unknown expiry (``token_expires_at is None``) answers False: we
        cannot judge it, and the platforms where "unknown" should mean "refresh
        anyway" are named explicitly in ``tasks.EXPIRY_BOOTSTRAP_PLATFORMS``.

        The window is a caller's decision because it means different things at
        different cadences. A user-triggered publish can afford the generous
        7-day view. An hourly background loop cannot: a Google access token
        lives one hour, so *any* window wider than that is permanently true and
        would spend a refresh call every hour on every account for nothing.
        """
        if not self.token_expires_at:
            return False
        from django.utils import timezone

        return self.token_expires_at < timezone.now() + window

    @property
    def is_token_expiring_soon(self) -> bool:
        """Token expires within 7 days."""
        from datetime import timedelta

        return self.token_expires_within(timedelta(days=7))

    @property
    def needs_reconnect(self) -> bool:
        return self.connection_status in (
            self.ConnectionStatus.DISCONNECTED,
            self.ConnectionStatus.ERROR,
        )

    def refresh_oauth_token(self, provider, *, enqueue_backfill: bool = True) -> str:
        """Refresh this account's OAuth access token via *provider* and persist it.

        Returns the new access token. Propagates whatever the provider's
        ``refresh_token`` raises so callers decide between degrading (publish
        engine keeps the old token) and aborting (composer endpoints 502).

        ``enqueue_backfill=False`` is for routine token rotations (publishing,
        analytics, inbox polls, and composer lookups). A rotation is not a new
        connection and must not queue a full historical analytics backfill.

        Serialize refreshes with a row lock on PostgreSQL or an account lock
        within the SQLite worker. A caller that waited for another refresh uses
        its persisted credentials rather than resubmitting the refresh token.
        """
        from datetime import timedelta

        from django.utils import timezone

        from .token_refresh import token_refresh_lock, token_refresh_transaction

        using = router.db_for_write(type(self), instance=self)
        accounts = type(self).objects.using(using)
        token_fields = ("oauth_access_token", "oauth_refresh_token", "token_expires_at")
        try:
            with token_refresh_lock(using, self.pk):
                with token_refresh_transaction(using):
                    current = accounts.select_for_update().get(pk=self.pk)
                    if self.platform == "youtube" and current.youtube_data_generation != self.youtube_data_generation:
                        from providers.exceptions import TokenExpiredError

                        raise TokenExpiredError("YouTube connection changed during token refresh", platform="youtube")
                if all(getattr(current, field) == getattr(self, field) for field in token_fields):
                    new_tokens = provider.refresh_token(current.oauth_refresh_token)
                    # SQLite has no row lock during HTTP. Discard a result if
                    # another process replaced the stored credentials meanwhile.
                    with token_refresh_transaction(using):
                        current = accounts.select_for_update().get(pk=self.pk)
                        if (
                            self.platform == "youtube"
                            and current.youtube_data_generation != self.youtube_data_generation
                        ):
                            from providers.exceptions import TokenExpiredError

                            raise TokenExpiredError(
                                "YouTube connection changed during token refresh", platform="youtube"
                            )
                        if all(getattr(current, field) == getattr(self, field) for field in token_fields):
                            current.oauth_access_token = new_tokens.access_token
                            if new_tokens.refresh_token:
                                current.oauth_refresh_token = new_tokens.refresh_token
                            if new_tokens.expires_in:
                                current.token_expires_at = timezone.now() + timedelta(seconds=new_tokens.expires_in)
                            current.connection_status = self.ConnectionStatus.CONNECTED
                            current._skip_analytics_backfill = not enqueue_backfill
                            current.save(using=using, update_fields=[*token_fields, "connection_status", "updated_at"])
                state_fields = [*token_fields, "connection_status"]
                if self.platform == "instagram":
                    # save() invalidates Instagram analytics on a rotation.
                    # Keep the caller's generation and cached verdicts in sync
                    # even though the locked instance performed that save.
                    if current.analytics_credentials_version != self.analytics_credentials_version:
                        self.__dict__.pop("_instagram_analytics_grant", None)
                        self.__dict__.pop("_instagram_analytics_status_recorded", None)
                    state_fields.extend(
                        [
                            "oauth_facebook_user_access_token",
                            "facebook_user_token_expires_at",
                            "analytics_credentials_version",
                            "analytics_status",
                            "analytics_error_reason",
                            "analytics_checked_at",
                            "analytics_needs_reconnect",
                        ]
                    )
                for field in state_fields:
                    setattr(self, field, getattr(current, field))
        except Exception as exc:
            if self.platform == "youtube":
                from .youtube_compliance import permanent_grant_rejection, purge_connection

                # Purge after the failed refresh transaction has rolled back.
                if permanent_grant_rejection(exc):
                    purge_connection(self, check_generation=True)
            raise
        return self.oauth_access_token

    # Platform character limits.
    #
    # Fallback (deliberate, documented like the quota fallback in
    # ``apps/api/limits.py``): a destination with no key here — today
    # only Zernio-LinkedIn, whose destination slug is ``"linkedin"``
    # while this map only carries ``"linkedin_personal"`` /
    # ``"linkedin_company"`` — falls back to 2200 via ``char_limit``
    # and to ``PLATFORM_FIELD_DEFAULTS`` via ``field_config``. No
    # LinkedIn number is guessed here: the personal/company flavour
    # of a ``"linkedin"`` destination is not known at this layer.
    PLATFORM_CHAR_LIMITS = {
        "facebook": 63206,
        "instagram": 2200,
        "instagram_login": 2200,
        "linkedin_personal": 3000,
        "linkedin_company": 3000,
        "tiktok": 2200,
        "youtube": 5000,
        "pinterest": 500,
        "threads": 500,
        "bluesky": 300,
        "google_business": 1500,
        "mastodon": 500,
        "devto": 25000,
    }

    @property
    def char_limit(self) -> int:
        return self.PLATFORM_CHAR_LIMITS.get(self.display_platform, 2200)

    @property
    def escaped_chars(self) -> str:
        """Characters this platform escapes, each costing two against the limit."""
        from providers import CAPTION_ESCAPED_CHARS

        return CAPTION_ESCAPED_CHARS.get(self.display_platform, "")

    def caption_wire_length(self, text: str) -> int:
        """Caption length as this platform counts it, after any escaping.

        LinkedIn escapes reserved characters in the commentary it publishes, so
        the typed length is not the length that counts against ``char_limit``.
        """
        from providers import caption_wire_length

        return caption_wire_length(self.display_platform, text)

    # Platform-specific field configuration (which platforms need extra fields)
    PLATFORM_FIELD_CONFIG: dict[str, dict[str, Any]] = {
        "youtube": {
            "needs_title": True,
            "title_max_length": 100,
            "title_label": "Video Title",
            "caption_label": "Description",
            "advanced_fields": ["made_for_kids", "privacy_status", "tags", "thumbnail"],
        },
        "pinterest": {
            "needs_title": True,
            "title_max_length": 100,
            "title_label": "Pin Title",
            "caption_label": "Description",
            "supports_first_comment": False,
            "advanced_fields": ["allow_comments", "show_similar_products", "alt_text", "cover_image"],
        },
        "tiktok": {
            "supports_first_comment": False,
        },
        "bluesky": {
            "supports_first_comment": False,
        },
        "google_business": {
            "supports_first_comment": False,
        },
        "devto": {
            "needs_title": True,
            "title_max_length": 128,
            "title_label": "Article Title",
            "caption_label": "Body (Markdown)",
            "supports_first_comment": False,
        },
    }

    PLATFORM_FIELD_DEFAULTS = {
        "needs_title": False,
        "title_max_length": 0,
        "title_label": "Title",
        "caption_label": "Caption",
        "supports_first_comment": True,
        "advanced_fields": [],
    }

    # Advanced fields Zernio's POST /v1/posts actually carries, by
    # destination platform. Everything else in the direct providers'
    # advanced lists has no Zernio padanan (YouTube thumbnail /
    # made_for_kids, Pinterest board / link / alt text / cover, ...) and
    # is dropped for via-Zernio accounts, so the composer never advertises
    # an option the transport silently discards. TikTok's panel options
    # (privacy, allow_*, is_aigc) are not advanced_fields; they are mapped
    # in providers.zernio.build_post_payload instead.
    ZERNIO_ADVANCED_FIELDS: dict[str, list[str]] = {
        "youtube": ["privacy_status", "tags"],
    }

    @property
    def field_config(self) -> dict:
        """Return field configuration for this platform.

        Title/caption/first-comment requirements follow the destination
        platform. For Zernio accounts ``advanced_fields`` keeps only the
        destination extras Zernio's POST /v1/posts actually carries
        (YouTube privacy_status/tags -> visibility/tags); the rest of the
        direct providers' extras (thumbnail, made_for_kids, board, ...)
        have no Zernio padanan and are dropped, so the composer never
        advertises an option the transport silently discards.
        ``supports_first_comment`` is likewise forced False for Zernio
        accounts: the transport has no comment endpoint, so it cannot
        honour a first comment even when the destination could.
        """
        cfg = {**self.PLATFORM_FIELD_DEFAULTS, **self.PLATFORM_FIELD_CONFIG.get(self.display_platform, {})}
        if self.is_via_zernio:
            allowed = self.ZERNIO_ADVANCED_FIELDS.get(self.display_platform, [])
            cfg["advanced_fields"] = [f for f in cfg["advanced_fields"] if f in allowed]
            # Zernio has no comment endpoint, so the transport cannot honour
            # a first comment even when the destination platform could.
            # Set here — the single source of truth — so every reader of
            # field_config (API schema, composer, engine via
            # supports_first_comment()) agrees instead of advertising a
            # first comment that would hang PENDING forever.
            cfg["supports_first_comment"] = False
        return cfg

    @property
    def keeps_platform_grant_on_disconnect(self) -> bool:
        """True when disconnecting here cannot revoke the platform's grant.

        The Facebook-Page flows share one grant across every Page and Instagram
        account that person connected, so the only endpoint that would revoke
        it takes all of them down at once — see
        ``FacebookProvider.revoke_token``. Instagram Login is excluded: its
        token belongs to the one account, so disconnect does revoke it.
        """
        return self.platform in {
            PlatformCredential.Platform.FACEBOOK,
            PlatformCredential.Platform.INSTAGRAM,
        }

    @property
    def revokes_platform_grant_on_disconnect(self) -> bool:
        """True when disconnecting also revokes our access on the platform.

        The disconnect confirmation promises exactly this, so it lists only the
        providers whose ``revoke_token`` calls a revocation endpoint with the
        token disconnect hands it. Left out, besides the Facebook-Page flows:
        Pinterest, Threads and DEV.to have no revocation call, and Bluesky's
        ``deleteSession`` wants the refresh JWT rather than the access JWT it
        gets — and the app password outlives any session regardless.
        """
        return self.platform in {
            PlatformCredential.Platform.YOUTUBE,
            PlatformCredential.Platform.GOOGLE_BUSINESS,
            PlatformCredential.Platform.TIKTOK,
            PlatformCredential.Platform.LINKEDIN_PERSONAL,
            PlatformCredential.Platform.LINKEDIN_COMPANY,
            PlatformCredential.Platform.MASTODON,
            PlatformCredential.Platform.INSTAGRAM_LOGIN,
        }

    @property
    def revocation_token(self) -> str:
        """The token disconnect should hand the provider's ``revoke_token``.

        Google refuses to revoke an expired access token, and ours lives an
        hour, so by the time someone disconnects it is usually dead and the
        grant would survive. The refresh token revokes the same grant and
        outlives it.
        """
        if (
            self.platform
            in {
                PlatformCredential.Platform.YOUTUBE,
                PlatformCredential.Platform.GOOGLE_BUSINESS,
            }
            and self.oauth_refresh_token
        ):
            return self.oauth_refresh_token
        return self.oauth_access_token

    def supports_first_comment(self) -> bool:
        """Whether this account can have a first comment posted by the worker.

        Most platforms answer purely from PLATFORM_FIELD_CONFIG. LinkedIn Personal
        is the exception: in OIDC mode the socialActions.CREATE endpoint returns
        403 ACCESS_DENIED because that endpoint is gated on Community Management
        API approval. Resolve credentials and check ``_oauth_mode`` for it.
        Zernio accounts always answer False: ``field_config`` forces the flag
        off because the transport has no comment endpoint.
        """
        if not self.field_config.get("supports_first_comment", True):
            return False
        if self.platform == "linkedin_personal":
            from apps.publisher.engine import _resolve_publish_credentials

            creds = _resolve_publish_credentials(self)
            if creds.get("_oauth_mode", "oidc") == "oidc":
                return False
        return True

    @property
    def platform_icon(self) -> str:
        """Short icon label for platform badges."""
        icons = {
            "facebook": "f",
            "instagram": "ig",
            "instagram_login": "ig",
            "linkedin_personal": "in",
            "linkedin_company": "in",
            "tiktok": "tk",
            "youtube": "yt",
            "pinterest": "pi",
            "threads": "th",
            "bluesky": "bs",
            "google_business": "gb",
            "mastodon": "ma",
            "devto": "dv",
        }
        return icons.get(self.display_platform, self.display_platform[:2])


class MastodonAppRegistration(models.Model):
    """Stores per-instance OAuth app registrations for Mastodon federation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    instance_url = models.URLField(max_length=500, unique=True)
    client_id = EncryptedTextField()
    client_secret = EncryptedTextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "social_accounts_mastodon_app_registration"

    def __str__(self):
        return self.instance_url


class LiveBroadcast(models.Model):
    """A YouTube Live broadcast created from the Live monitor.

    One row = one ``liveBroadcasts`` resource bound to one ``liveStreams``
    ingest. The broadcast id IS the watch-URL video id. The RTMP stream
    key is a credential: it lives in :class:`EncryptedTextField`, is never
    written to logs, and the UI only ever shows it masked — except once,
    on the page right after creation, and on explicit per-click reveal.
    The ingest *address* (``rtmp://a.rtmp.youtube.com/live2``) is public
    knowledge and is stored in plain text.
    """

    class Status(models.TextChoices):
        READY = "ready", "Ready"
        TESTING = "testing", "Testing"
        LIVE = "live", "Live"
        COMPLETE = "complete", "Complete"
        CANCELLED = "cancelled", "Cancelled"
        ERROR = "error", "Error"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="live_broadcasts",
    )
    social_account = models.ForeignKey(
        SocialAccount,
        on_delete=models.CASCADE,
        related_name="live_broadcasts",
    )
    broadcast_id = models.CharField(max_length=64, unique=True)
    stream_id = models.CharField(max_length=64, blank=True, default="")
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    privacy_status = models.CharField(max_length=10, default="private")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.READY)
    scheduled_start = models.DateTimeField(blank=True, null=True)
    actual_start = models.DateTimeField(blank=True, null=True)
    actual_end = models.DateTimeField(blank=True, null=True)
    last_viewers = models.PositiveIntegerField(blank=True, null=True)
    last_stream_status = models.CharField(max_length=20, blank=True, default="")
    last_health_status = models.CharField(max_length=20, blank=True, default="")
    stream_url = models.CharField(
        max_length=500,
        blank=True,
        default="",
        help_text="RTMP ingestion address (no key in it).",
    )
    stream_key = EncryptedTextField(blank=True, default="")
    # True once the full key has been shown on the post-creation page.
    # The monitor only ever shows the masked key after that; copying needs
    # an explicit per-click reveal.
    key_shown = models.BooleanField(default=False)
    last_error = models.CharField(max_length=500, blank=True, default="")
    last_checked_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "social_accounts_live_broadcast"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["workspace", "status"]),
            models.Index(fields=["social_account", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.title} ({self.broadcast_id})"

    @property
    def watch_url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.broadcast_id}"

    @property
    def masked_stream_key(self) -> str:
        """Stream key with only the last 4 characters visible (for display)."""
        from providers.youtube import mask_stream_key

        return mask_stream_key(self.stream_key or "")

    @property
    def is_active(self) -> bool:
        return self.status in (self.Status.TESTING, self.Status.LIVE)

    @property
    def is_actionable(self) -> bool:
        """READY/TESTING/LIVE: shown on the monitor active card.

        ``is_active`` deliberately stays TESTING/LIVE only - the ingest
        is actually running / on air - because the API detail read, the
        MCP list and the JSON poller use it to decide whether to spend a
        YouTube status poll. A READY broadcast is not active in that
        sense, but the operator can act on it from the monitor (Start
        test, or Go Live directly without testing first), so the monitor
        card must include it. Kept as a separate property so widening
        the card does not silently change polling/quota behaviour
        elsewhere. COMPLETE/CANCELLED/ERROR have no monitor actions.
        """
        return self.status in (self.Status.READY, self.Status.TESTING, self.Status.LIVE)

    @property
    def uptime(self):
        """Seconds since the actual start, or None when not started."""
        if not self.actual_start:
            return None
        end = self.actual_end or timezone.now()
        return max(0, int((end - self.actual_start).total_seconds()))


class PlatformVisibility(models.Model):
    """Site-wide toggle controlling which platforms appear on the connect page."""

    platform = models.CharField(
        max_length=30,
        choices=PlatformCredential.Platform.choices,
        unique=True,
    )
    is_visible = models.BooleanField(
        default=True,
        help_text="If unchecked, this platform is hidden from the connect page.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "social_accounts_platform_visibility"
        verbose_name = "Connect page platform"
        verbose_name_plural = "Connect page platforms"
        ordering = ["platform"]

    def __str__(self):
        return f"{self.get_platform_display()} ({'visible' if self.is_visible else 'hidden'})"

    @classmethod
    def visible_choices(cls) -> list[tuple[str, str]]:
        """``Platform.choices`` minus the platforms an admin has hidden.

        The single source for "what can be connected" — the connect page and
        the sidebar's connect shortcuts both read it, so they can't drift
        (the sidebar used to keep its own hand-maintained copy, which had
        fallen behind by two platforms). Platforms without a row default to
        visible, matching ``is_visible``.
        """
        hidden = set(cls.objects.filter(is_visible=False).values_list("platform", flat=True))
        return [(value, label) for value, label in PlatformCredential.Platform.choices if value not in hidden]


class AnalyticsPlatformConfig(models.Model):
    """Site-wide toggle controlling which platforms are enabled for the
    Analytics feature.

    App-review timelines for the new analytics scopes (Meta, TikTok) are
    unpredictable, so admins flip platforms on as their approvals land. If
    no rows have ``is_enabled=True`` the Analytics sidebar item is hidden
    entirely (see ``apps.common.context_processors.sidebar_context``).
    """

    platform = models.CharField(
        max_length=30,
        choices=PlatformCredential.Platform.choices,
        unique=True,
    )
    is_enabled = models.BooleanField(
        default=True,
        help_text="If unchecked, this platform is excluded from the Analytics feature.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    # Not a column: ``apps.analytics.signals.stash_previous_enabled`` writes the
    # stored ``is_enabled`` here on pre_save so post_save can spot an off → on
    # flip. Declared so that write type-checks; ``None`` means "not stashed",
    # which is what the post_save reader already defaults to.
    _previously_enabled: bool | None = None

    class Meta:
        db_table = "social_accounts_analytics_platform_config"
        verbose_name = "Analytics platform"
        verbose_name_plural = "Analytics platforms"
        ordering = ["platform"]

    def __str__(self):
        return f"{self.get_platform_display()} ({'enabled' if self.is_enabled else 'disabled'})"

    @classmethod
    def enabled_platforms(cls) -> list[str]:
        """Return the list of platform slugs with analytics enabled.

        A platform with no row counts as enabled, matching ``is_enabled``'s
        default and the fact that the admin forbids adding or deleting rows —
        so a missing row always means "nothing has been said about this
        platform", never "an admin turned it off". Without that, every platform
        slug added after the seed migration (``devto`` was the first) had its
        analytics silently switched off, which is invisible from the admin
        because the platform isn't listed there either.

        Driven off ``Platform.choices`` rather than the table, so a row left
        behind by a renamed slug can't leak into the result.
        """
        rows = dict(cls.objects.values_list("platform", "is_enabled"))
        return [value for value, _label in PlatformCredential.Platform.choices if rows.get(value, True)]
