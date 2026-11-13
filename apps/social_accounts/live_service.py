"""Shared YouTube Live service layer.

The web monitor (``live_views``), the Agent API router
(``apps.api.routers.live``) and the MCP tools (``apps.mcp.handlers``)
all go through these functions, so the three surfaces cannot drift:
one scope check, one create/bind/cleanup flow, one transition rule,
one status poll (throttled to 45 s), one wire shape.

Nothing here knows about HTTP, redirects or flash messages — callers
map ``LiveServiceError`` onto their own error shape. The stream key is
a credential: the create / recreate calls return it in full exactly
once, and every other serialization carries only the masked form.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from providers.youtube import LIVE_SCOPE, mask_stream_key, rtmps_ingestion_url

from .models import LiveBroadcast, SocialAccount
from .provider_factory import _get_provider_for_platform, apply_analytics_scope_flag

logger = logging.getLogger(__name__)

# Live status calls cost 1 quota unit each from the regular 10,000-unit
# Data API pool, and a monitor page left open all day would spend the
# whole deployment's budget on one broadcast. Two brakes: the browser
# polls at most once a minute (and only while a broadcast is testing or
# live), and the server refuses to re-poll YouTube more often than this.
LIVE_REFRESH_MIN_INTERVAL = timedelta(seconds=45)
LIVE_POLL_INTERVAL_SECONDS = 60
LIVE_SCOPE_CACHE_TIMEOUT = 3600  # 1 hour; a grant survives token refreshes

LIVE_TRANSITION_TARGETS = ("testing", "live", "complete")


class LiveServiceError(Exception):
    """A Live operation failed, with a caller-facing message.

    ``status_code`` is the HTTP status the REST router should answer
    with (MCP maps it onto a JSON-RPC error with the same message).
    ``insufficient_scope`` marks the one failure the operator can fix
    themselves: the token lacks the full YouTube scope, so the account
    must be reconnected for Live.
    """

    def __init__(self, message: str, *, status_code: int = 502, insufficient_scope: bool = False):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.insufficient_scope = insufficient_scope


def provider_and_token(account: SocialAccount):
    """Build the YouTube provider for ``account`` with a usably-fresh token.

    Best-effort refresh (publish-engine pattern): on refresh failure we
    keep the old token and let the API call surface the real error.
    """
    provider = _get_provider_for_platform("youtube", account.workspace.organization_id)
    apply_analytics_scope_flag(provider, "youtube")
    token = account.oauth_access_token
    if account.token_expires_within(timedelta(days=1)) and account.oauth_refresh_token:
        try:
            token = account.refresh_oauth_token(provider)
        except Exception:
            logger.warning("Live: token refresh failed for %s", account, exc_info=True)
    return provider, token


def live_ready(account: SocialAccount) -> bool | None:
    """Whether ``account``'s grant carries the full YouTube (Live) scope.

    Read from Google's tokeninfo endpoint (no Data API quota), with a
    positive result cached for an hour per account. A negative result
    is never cached, so a reconnect with the Live scope takes effect
    on the next check. None means "could not be inspected" — expired
    token, revoked grant, network failure — not "missing".
    """
    cache_key = f"yt-live-scope:{account.pk}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    provider, token = provider_and_token(account)
    try:
        scopes = provider.get_granted_scopes(token)
    except Exception:
        logger.warning("Live: scope inspection failed for %s", account, exc_info=True)
        return None
    if scopes is None:
        return None
    ready = LIVE_SCOPE in scopes
    # Only a positive result is cached: a negative one goes stale the
    # moment the account is reconnected with the Live scope, and would
    # otherwise keep failing creates for up to an hour.
    if ready:
        cache.set(cache_key, ready, LIVE_SCOPE_CACHE_TIMEOUT)
    return ready


def parse_api_datetime(value):
    if not value:
        return None
    parsed = parse_datetime(value)
    if parsed is not None and timezone.is_naive(parsed):
        return timezone.make_aware(parsed, timezone.utc)
    return parsed


def refresh_broadcast_status(broadcast: LiveBroadcast, *, force: bool = False) -> bool:
    """Poll YouTube for one broadcast's live state and persist it.

    Returns True when a poll actually happened. Throttled to
    ``LIVE_REFRESH_MIN_INTERVAL`` unless ``force`` — the dashboard and
    the JSON poller both call this, and without the brake a page left open
    would spend quota on every render.
    """
    now = timezone.now()
    if (
        not force
        and broadcast.last_checked_at is not None
        and now - broadcast.last_checked_at < LIVE_REFRESH_MIN_INTERVAL
    ):
        return False
    provider, token = provider_and_token(broadcast.social_account)
    try:
        info = provider.get_live_broadcast(token, broadcast_id=broadcast.broadcast_id)
        if info:
            broadcast.status = info.get("status", broadcast.status)
            broadcast.actual_start = parse_api_datetime(info.get("actual_start_time")) or broadcast.actual_start
            broadcast.actual_end = parse_api_datetime(info.get("actual_end_time")) or broadcast.actual_end
            if info.get("scheduled_start_time"):
                broadcast.scheduled_start = parse_api_datetime(info["scheduled_start_time"])
        if broadcast.stream_id:
            stream = provider.get_live_stream(token, stream_id=broadcast.stream_id)
            if stream:
                broadcast.last_stream_status = stream.get("stream_status", "")
                broadcast.last_health_status = stream.get("health_status", "")
        if broadcast.status == LiveBroadcast.Status.LIVE:
            details = provider.get_live_video_details(token, video_id=broadcast.broadcast_id)
            if details:
                if details.get("concurrent_viewers") is not None:
                    broadcast.last_viewers = details["concurrent_viewers"]
                broadcast.actual_start = (
                    parse_api_datetime(details.get("actual_start_time")) or broadcast.actual_start
                )
                broadcast.actual_end = parse_api_datetime(details.get("actual_end_time")) or broadcast.actual_end
        broadcast.last_error = ""
    except Exception as exc:
        # A failed poll must not take the row down with it: keep the last
        # known state, record the failure, and let the next poll retry.
        broadcast.last_error = str(exc)[:500]
        logger.warning("Live: status poll failed for %s: %s", broadcast.broadcast_id, exc, exc_info=True)
    broadcast.last_checked_at = now
    broadcast.save(
        update_fields=[
            "status",
            "scheduled_start",
            "actual_start",
            "actual_end",
            "last_viewers",
            "last_stream_status",
            "last_health_status",
            "last_error",
            "last_checked_at",
            "updated_at",
        ]
    )
    return True


def is_insufficient_scope(exc: Exception) -> bool:
    """Best-effort recognition of "token lacks the Live scope" failures."""
    try:
        from apps.analytics.tasks import _is_insufficient_scope as _analytics_check
    except Exception:
        return "insufficient" in str(exc).lower()
    return _analytics_check(exc)


def _youtube_error(exc: Exception) -> LiveServiceError:
    """Map a YouTube call failure onto the service error shape."""
    if is_insufficient_scope(exc):
        return LiveServiceError(
            "YouTube refused: this token does not have the Live permission "
            "(full youtube scope). Reconnect the account for Live and try again.",
            status_code=403,
            insufficient_scope=True,
        )
    return LiveServiceError(f"YouTube request failed: {exc}", status_code=502)


def create_live_broadcast(
    workspace,
    account: SocialAccount,
    *,
    title: str,
    description: str = "",
    privacy_status: str = "private",
    resolution: str = "1080p",
    frame_rate: str = "30fps",
    scheduled_start=None,
) -> tuple[LiveBroadcast, str]:
    """Create ingest stream + broadcast, bind them, save the row.

    Returns ``(broadcast, stream_key)`` — the key in full, exactly once;
    the caller is responsible for showing it once and then marking the
    row ``key_shown`` (the web created-page and the API create response
    both do this). Nothing here goes live: the broadcast starts in
    ``ready`` and only a later explicit transition starts it.
    """
    title = (title or "").strip()
    if not title:
        raise LiveServiceError("A title is required to create a live broadcast.", status_code=422)
    if live_ready(account) is False:
        raise LiveServiceError(
            "This YouTube account has not granted the Live permission yet. "
            "Reconnect it for Live first.",
            status_code=403,
            insufficient_scope=True,
        )

    provider, token = provider_and_token(account)
    stream = broadcast = None
    try:
        stream = provider.create_live_stream(token, title=title, resolution=resolution, frame_rate=frame_rate)
        broadcast = provider.create_live_broadcast(
            token,
            title=title,
            description=description,
            privacy_status=privacy_status,
            scheduled_start_time=scheduled_start.isoformat().replace("+00:00", "Z") if scheduled_start else None,
        )
        provider.bind_live_broadcast(token, broadcast_id=broadcast["broadcast_id"], stream_id=stream["stream_id"])
    except LiveServiceError:
        raise
    except ValueError as exc:
        # Provider-side validation (bad resolution / frame rate / privacy).
        raise LiveServiceError(str(exc), status_code=422) from exc
    except Exception as exc:
        # Never leave a half-created pair behind on YouTube: a broadcast
        # without its stream (or vice versa) still shows up in the
        # channel's Live dashboard as a dead entry.
        if broadcast:
            provider.delete_live_broadcast(token, broadcast_id=broadcast["broadcast_id"])
        if stream:
            provider.delete_live_stream(token, stream_id=stream["stream_id"])
        logger.warning("Live: create failed for %s: %s", account, exc, exc_info=True)
        raise _youtube_error(exc) from exc

    row = LiveBroadcast.objects.create(
        workspace=workspace,
        social_account=account,
        broadcast_id=broadcast["broadcast_id"],
        stream_id=stream["stream_id"],
        title=title,
        description=description,
        privacy_status=privacy_status,
        status=broadcast.get("status", LiveBroadcast.Status.READY),
        scheduled_start=scheduled_start,
        stream_url=stream.get("ingestion_address", ""),
        stream_key=stream.get("stream_name", ""),
    )
    return row, stream.get("stream_name", "")


def transition_live_broadcast(broadcast: LiveBroadcast, target: str) -> LiveBroadcast:
    """Move a broadcast to ``testing`` / ``live`` / ``complete``."""
    if target not in LIVE_TRANSITION_TARGETS:
        raise LiveServiceError(
            f"Unknown live transition: {target!r}. Use one of: {', '.join(LIVE_TRANSITION_TARGETS)}.",
            status_code=422,
        )
    provider, token = provider_and_token(broadcast.social_account)
    try:
        info = provider.transition_live_broadcast(
            token, broadcast_id=broadcast.broadcast_id, broadcast_status=target
        )
    except Exception as exc:
        logger.warning(
            "Live: transition to %s failed for %s: %s", target, broadcast.broadcast_id, exc, exc_info=True
        )
        raise _youtube_error(exc) from exc
    broadcast.status = info.get("status", broadcast.status)
    broadcast.actual_start = parse_api_datetime(info.get("actual_start_time")) or broadcast.actual_start
    broadcast.actual_end = parse_api_datetime(info.get("actual_end_time")) or broadcast.actual_end
    broadcast.last_error = ""
    broadcast.last_checked_at = timezone.now()
    broadcast.save(update_fields=["status", "actual_start", "actual_end", "last_error", "last_checked_at", "updated_at"])
    return broadcast


def delete_live_broadcast(broadcast: LiveBroadcast) -> None:
    """Delete the broadcast (and its ingest stream) on YouTube and here.

    YouTube deletions are best-effort (the provider swallows failures and
    returns False) — a broadcast already gone on YouTube must not block
    removing the local row.
    """
    provider, token = provider_and_token(broadcast.social_account)
    provider.delete_live_broadcast(token, broadcast_id=broadcast.broadcast_id)
    if broadcast.stream_id:
        provider.delete_live_stream(token, stream_id=broadcast.stream_id)
    broadcast.delete()


def recreate_live_stream(broadcast: LiveBroadcast) -> tuple[LiveBroadcast, str]:
    """Replace a broadcast's ingest stream (the only way to reset its key).

    YouTube has no "reset stream key" call: the old stream is deleted and
    a fresh one is created and bound. Only while the broadcast is still
    ``ready`` or ``testing`` — never mid-broadcast. Returns
    ``(broadcast, new_stream_key)`` — the key in full, exactly once.
    """
    if broadcast.status not in (LiveBroadcast.Status.READY, LiveBroadcast.Status.TESTING):
        raise LiveServiceError(
            "The stream can only be replaced before the broadcast goes live.",
            status_code=422,
        )
    provider, token = provider_and_token(broadcast.social_account)
    stream = None
    try:
        stream = provider.create_live_stream(token, title=broadcast.title)
        provider.bind_live_broadcast(token, broadcast_id=broadcast.broadcast_id, stream_id=stream["stream_id"])
    except Exception as exc:
        # A stream created but never bound is an orphan on YouTube: drop
        # it, mirroring create_live_broadcast's cleanup. The old stream
        # stays untouched — it is still the broadcast's ingest.
        if stream:
            provider.delete_live_stream(token, stream_id=stream["stream_id"])
        logger.warning("Live: stream replacement failed for %s: %s", broadcast.broadcast_id, exc, exc_info=True)
        raise _youtube_error(exc) from exc
    if broadcast.stream_id:
        provider.delete_live_stream(token, stream_id=broadcast.stream_id)
    broadcast.stream_id = stream["stream_id"]
    broadcast.stream_url = stream.get("ingestion_address", "")
    broadcast.stream_key = stream.get("stream_name", "")
    broadcast.key_shown = False
    broadcast.last_stream_status = stream.get("stream_status", "")
    broadcast.last_health_status = stream.get("health_status", "")
    broadcast.save(
        update_fields=[
            "stream_id",
            "stream_url",
            "stream_key",
            "key_shown",
            "last_stream_status",
            "last_health_status",
            "updated_at",
        ]
    )
    return broadcast, stream.get("stream_name", "")


def mark_key_shown(broadcast: LiveBroadcast) -> None:
    """Record that the full stream key has been disclosed exactly once."""
    if not broadcast.key_shown:
        broadcast.key_shown = True
        broadcast.save(update_fields=["key_shown", "updated_at"])


def serialize_live_broadcast(broadcast: LiveBroadcast, *, stream_key: str | None = None) -> dict:
    """The one wire shape for a broadcast, shared by REST and MCP.

    ``stream_key`` is the full key, passed only by the create / recreate
    responses that disclose it once. Every other call leaves it out and
    the response carries only ``masked_stream_key``.
    """
    data = {
        "id": str(broadcast.id),
        "broadcast_id": broadcast.broadcast_id,
        "stream_id": broadcast.stream_id,
        "social_account_id": str(broadcast.social_account_id),
        "social_account_name": broadcast.social_account.account_name if broadcast.social_account_id else "",
        "title": broadcast.title,
        "description": broadcast.description,
        "privacy_status": broadcast.privacy_status,
        "status": broadcast.status,
        "scheduled_start": broadcast.scheduled_start.isoformat() if broadcast.scheduled_start else None,
        "actual_start": broadcast.actual_start.isoformat() if broadcast.actual_start else None,
        "actual_end": broadcast.actual_end.isoformat() if broadcast.actual_end else None,
        "stream_status": broadcast.last_stream_status,
        "health_status": broadcast.last_health_status,
        "viewers": broadcast.last_viewers,
        "uptime_seconds": broadcast.uptime,
        "watch_url": broadcast.watch_url,
        "rtmps_url": rtmps_ingestion_url(broadcast.stream_url),
        "masked_stream_key": broadcast.masked_stream_key,
        "stream_key": stream_key,
        "key_shown": broadcast.key_shown,
        "last_error": broadcast.last_error,
        "last_checked_at": broadcast.last_checked_at.isoformat() if broadcast.last_checked_at else None,
        "created_at": broadcast.created_at.isoformat() if broadcast.created_at else None,
        "updated_at": broadcast.updated_at.isoformat() if broadcast.updated_at else None,
    }
    return data


__all__ = [
    "LIVE_POLL_INTERVAL_SECONDS",
    "LIVE_REFRESH_MIN_INTERVAL",
    "LIVE_TRANSITION_TARGETS",
    "LiveServiceError",
    "create_live_broadcast",
    "delete_live_broadcast",
    "is_insufficient_scope",
    "live_ready",
    "mark_key_shown",
    "mask_stream_key",
    "parse_api_datetime",
    "provider_and_token",
    "recreate_live_stream",
    "refresh_broadcast_status",
    "serialize_live_broadcast",
    "transition_live_broadcast",
]

