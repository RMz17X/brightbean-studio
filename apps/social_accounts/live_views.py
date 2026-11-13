"""YouTube Live monitor: create, watch and end live broadcasts.

One page per workspace (``/workspace/<id>/live/``) plus the actions it
needs. No real broadcast is ever created by the tests, and nothing here
publishes a video: a broadcast only goes live when the operator presses
"Go Live" with an encoder already streaming to the ingest.

Auth model mirrors the rest of the app: ``login_required`` plus a
workspace-membership check (``_get_workspace``), and every action is
scoped to that workspace's own YouTube accounts and broadcasts.

All YouTube work lives in ``live_service`` (shared with the Agent API
router and the MCP tools); these views only translate between HTTP
(forms / redirects / flash messages) and the service. The module-level
``_live_ready`` / ``_refresh_broadcast_status`` aliases exist so the
existing tests can keep patching this module's names.

Scope note — read before touching the OAuth flow: the Live Streaming API
needs the full ``https://www.googleapis.com/auth/youtube`` scope
(``providers.youtube.LIVE_SCOPE``), which the regular upload-only connect
does NOT request. ``live_reconnect`` starts a fresh OAuth flow with
``include_live_scopes=True``; the shared ``oauth_callback`` then stores
the new (wider) grant on the same account. Until the channel owner does
that, every Live call fails with ``insufficientPermissions`` and the
monitor shows the re-authorize banner instead of a generic error.
"""

from __future__ import annotations

import logging
import secrets

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_GET, require_POST
from django_ratelimit.decorators import ratelimit

from apps.members.models import WorkspaceMembership
from apps.workspaces.models import Workspace
from providers.youtube import LIVE_SCOPE, YouTubeProvider, rtmps_ingestion_url

from . import live_service
from .live_service import (
    LIVE_POLL_INTERVAL_SECONDS,
    LiveServiceError,
    is_insufficient_scope as _is_insufficient_scope,
)
from .live_service import (
    live_ready as _live_ready,
)
from .live_service import (
    parse_api_datetime as _parse_api_datetime,
)
from .live_service import (
    provider_and_token as _provider_and_token,
)
from .live_service import (
    refresh_broadcast_status as _refresh_broadcast_status,
)
from .models import LiveBroadcast, SocialAccount
from .oauth_pkce import issue_pkce_verifier, pkce_kwargs
from .provider_factory import _get_provider_for_platform, apply_analytics_scope_flag
from .views import OAUTH_SESSION_KEY, _build_redirect_uri, _sign_state

logger = logging.getLogger(__name__)

# Re-exported for the existing tests, which patch these names on this
# module (``apps.social_accounts.live_views._live_ready`` etc.).
_LIVE_REFRESH_MIN_INTERVAL = live_service.LIVE_REFRESH_MIN_INTERVAL


def _get_workspace(request, workspace_id):
    """Resolve workspace and enforce membership check (calendar pattern)."""
    workspace = get_object_or_404(Workspace, id=workspace_id)
    if not request.user.is_authenticated:
        raise PermissionDenied("Authentication required.")
    if not WorkspaceMembership.objects.filter(user=request.user, workspace=workspace).exists():
        raise PermissionDenied("You are not a member of this workspace.")
    return workspace


def _youtube_accounts(workspace):
    return (
        SocialAccount.objects.for_workspace(workspace.id)
        .filter(platform="youtube", connection_status=SocialAccount.ConnectionStatus.CONNECTED)
        .order_by("account_name")
    )


def _broadcast_payload(broadcast: LiveBroadcast) -> dict:
    return {
        "id": str(broadcast.id),
        "broadcast_id": broadcast.broadcast_id,
        "title": broadcast.title,
        "status": broadcast.status,
        "stream_status": broadcast.last_stream_status,
        "health_status": broadcast.last_health_status,
        "viewers": broadcast.last_viewers,
        "actual_start": broadcast.actual_start.isoformat() if broadcast.actual_start else None,
        "uptime_seconds": broadcast.uptime,
        "watch_url": broadcast.watch_url,
        "last_error": broadcast.last_error,
        "last_checked_at": broadcast.last_checked_at.isoformat() if broadcast.last_checked_at else None,
    }


# ----------------------------------------------------------------------
# Monitor
# ----------------------------------------------------------------------


@login_required
def live_dashboard(request, workspace_id):
    """Live monitor: broadcasts, per-account Live readiness, create form.

    The active card lists every ``is_actionable`` broadcast (READY as
    well as TESTING/LIVE), so a READY broadcast offers Go Live directly
    without Start test first. The refresh poll still only runs for
    ``is_active`` (TESTING/LIVE): a READY row is idle, and polling it
    would spend YouTube quota for no new information.
    """
    workspace = _get_workspace(request, workspace_id)
    accounts = list(_youtube_accounts(workspace))
    broadcasts = list(
        LiveBroadcast.objects.for_workspace(workspace.id).select_related("social_account").order_by("-created_at")
    )
    for broadcast in broadcasts:
        if broadcast.is_active:
            _refresh_broadcast_status(broadcast)
    readiness = {}
    for account in accounts:
        ready = _live_ready(account)
        readiness[str(account.pk)] = ready
        # Plain attributes for the template (no dict lookup by variable key).
        account.live_ready = ready
    for broadcast in broadcasts:
        # Plain attribute for the template; the DB stores the rtmp:// form.
        broadcast.rtmps_url = rtmps_ingestion_url(broadcast.stream_url)
    context = {
        "workspace": workspace,
        "workspace_id": workspace_id,
        "accounts": accounts,
        "readiness": readiness,
        "broadcasts": broadcasts,
        # READY included: the card is where Go Live lives, and the Go
        # Live form in the template already accepts ready/testing.
        "active_broadcasts": [b for b in broadcasts if b.is_actionable],
        "live_scope": LIVE_SCOPE,
        "resolutions": YouTubeProvider.LIVE_RESOLUTIONS,
        "frame_rates": YouTubeProvider.LIVE_FRAME_RATES,
        "poll_interval_seconds": LIVE_POLL_INTERVAL_SECONDS,
    }
    return render(request, "social_accounts/live/dashboard.html", context)


@login_required
@require_GET
def live_status(request, workspace_id):
    """JSON status for the monitor's auto-refresh (active broadcasts only)."""
    workspace = _get_workspace(request, workspace_id)
    # READY deliberately excluded: the JSON poller only tracks
    # testing/live; a READY card shows its last known state and needs no
    # YouTube poll (see live_dashboard docstring).
    broadcasts = LiveBroadcast.objects.for_workspace(workspace.id).filter(
        status__in=(LiveBroadcast.Status.TESTING, LiveBroadcast.Status.LIVE)
    )
    payload = []
    for broadcast in broadcasts:
        _refresh_broadcast_status(broadcast)
        broadcast.refresh_from_db()
        payload.append(_broadcast_payload(broadcast))
    return JsonResponse({"broadcasts": payload, "poll_interval_seconds": LIVE_POLL_INTERVAL_SECONDS})


# ----------------------------------------------------------------------
# Create
# ----------------------------------------------------------------------


@login_required
@require_POST
def live_create(request, workspace_id):
    """Create ingest stream + broadcast, bind them, save the row.

    Nothing here goes live: the broadcast starts in ``ready`` and only a
    later explicit transition (after the encoder is streaming) starts it.
    """
    workspace = _get_workspace(request, workspace_id)
    try:
        account = get_object_or_404(
            SocialAccount,
            pk=request.POST.get("account_id"),
            workspace=workspace,
            platform="youtube",
        )
    except ValidationError:
        # A non-UUID account_id from POST data raises ValidationError,
        # not DoesNotExist — answer 404, not 500.
        raise Http404("No YouTube account matches the given id.") from None
    title = (request.POST.get("title") or "").strip()
    if len(title) > 100:
        messages.error(request, "Live title must be at most 100 characters.")
        return redirect("live:dashboard", workspace_id=workspace_id)
    description = (request.POST.get("description") or "").strip()
    privacy_status = request.POST.get("privacy_status") or "private"
    resolution = request.POST.get("resolution") or "1080p"
    frame_rate = request.POST.get("frame_rate") or "30fps"
    scheduled_raw = (request.POST.get("scheduled_start") or "").strip()
    scheduled_start = None
    if scheduled_raw:
        parsed = parse_datetime(scheduled_raw)
        if parsed is not None:
            if timezone.is_naive(parsed):
                parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
            scheduled_start = parsed
    try:
        row, _stream_key = live_service.create_live_broadcast(
            workspace,
            account,
            title=title,
            description=description,
            privacy_status=privacy_status,
            resolution=resolution,
            frame_rate=frame_rate,
            scheduled_start=scheduled_start,
        )
    except LiveServiceError as exc:
        if exc.insufficient_scope:
            messages.error(
                request,
                "YouTube refused: this token does not have the Live permission "
                "(full youtube scope). Reconnect the account for Live and try again.",
            )
        else:
            messages.error(request, f"Could not create the live broadcast: {exc.message}")
        return redirect("live:dashboard", workspace_id=workspace_id)
    messages.success(request, f"Live broadcast “{title}” created. Paste the RTMP details into OBS, then go live.")
    return redirect("live:created", workspace_id=workspace_id, broadcast_id=row.pk)


@login_required
def live_created(request, workspace_id, broadcast_id):
    """Post-creation page: RTMPS URL + stream key, shown in full once."""
    workspace = _get_workspace(request, workspace_id)
    broadcast = get_object_or_404(LiveBroadcast, pk=broadcast_id, workspace=workspace)
    show_key = not broadcast.key_shown
    if show_key:
        broadcast.key_shown = True
        broadcast.save(update_fields=["key_shown", "updated_at"])
    context = {
        "workspace": workspace,
        "workspace_id": workspace_id,
        "broadcast": broadcast,
        "show_key": show_key,
        "rtmps_url": rtmps_ingestion_url(broadcast.stream_url),
        "masked_key": broadcast.masked_stream_key,
    }
    return render(request, "social_accounts/live/created.html", context)


# ----------------------------------------------------------------------
# Transitions / stream management
# ----------------------------------------------------------------------


@login_required
@require_POST
def live_transition(request, workspace_id, broadcast_id):
    """Test / Go Live / Stop a broadcast (one explicit operator action each)."""
    workspace = _get_workspace(request, workspace_id)
    broadcast = get_object_or_404(LiveBroadcast, pk=broadcast_id, workspace=workspace)
    action = request.POST.get("action", "")
    mapping = {"test": "testing", "go_live": "live", "stop": "complete"}
    if action not in mapping:
        messages.error(request, "Unknown live action.")
        return redirect("live:dashboard", workspace_id=workspace_id)
    target = mapping[action]
    try:
        live_service.transition_live_broadcast(broadcast, target)
    except LiveServiceError as exc:
        if exc.insufficient_scope:
            messages.error(
                request,
                "YouTube refused: this token does not have the Live permission. "
                "Reconnect the account for Live and try again.",
            )
        else:
            messages.error(request, f"Could not move the broadcast to {target}: {exc.message}")
        return redirect("live:dashboard", workspace_id=workspace_id)
    if target == "live":
        messages.success(request, f"“{broadcast.title}” is now LIVE.")
    elif target == "complete":
        messages.success(request, f"“{broadcast.title}” ended.")
    else:
        messages.success(request, f"“{broadcast.title}” is now in test mode — check the preview, then go live.")
    return redirect("live:dashboard", workspace_id=workspace_id)


@login_required
@require_POST
def live_delete(request, workspace_id, broadcast_id):
    """Delete the broadcast (and its ingest stream) on YouTube and here."""
    workspace = _get_workspace(request, workspace_id)
    broadcast = get_object_or_404(LiveBroadcast, pk=broadcast_id, workspace=workspace)
    live_service.delete_live_broadcast(broadcast)
    messages.success(request, f"Live broadcast “{broadcast.title}” deleted.")
    return redirect("live:dashboard", workspace_id=workspace_id)


@login_required
@require_POST
def live_recreate_stream(request, workspace_id, broadcast_id):
    """Replace a broadcast's ingest stream (the only way to reset its key).

    YouTube has no "reset stream key" call: the old stream is deleted and
    a fresh one is created and bound. Only while the broadcast is still
    ``ready`` or ``testing`` — never mid-broadcast.
    """
    workspace = _get_workspace(request, workspace_id)
    broadcast = get_object_or_404(LiveBroadcast, pk=broadcast_id, workspace=workspace)
    try:
        live_service.recreate_live_stream(broadcast)
    except LiveServiceError as exc:
        messages.error(request, exc.message)
        return redirect("live:dashboard", workspace_id=workspace_id)
    messages.success(request, "New ingest stream created — the old stream key no longer works.")
    return redirect("live:created", workspace_id=workspace_id, broadcast_id=broadcast.pk)


@login_required
@require_POST
@ratelimit(key="user", rate="10/m", block=True)
def live_reveal_key(request, workspace_id, broadcast_id):
    """Return the stream key for one explicit copy click (JSON, never logged).

    The key is never rendered in page HTML: the copy button POSTs here and
    copies the response straight to the clipboard.
    """
    workspace = _get_workspace(request, workspace_id)
    broadcast = get_object_or_404(LiveBroadcast, pk=broadcast_id, workspace=workspace)
    logger.info("Live: stream key revealed for broadcast %s by %s", broadcast.broadcast_id, request.user)
    return JsonResponse(
        {
            "stream_key": broadcast.stream_key or "",
            "rtmps_url": rtmps_ingestion_url(broadcast.stream_url),
        }
    )


# ----------------------------------------------------------------------
# Live re-authorize (wider OAuth scope)
# ----------------------------------------------------------------------


@login_required
@require_POST
def live_reconnect(request, workspace_id, account_id):
    """Start a YouTube OAuth flow that ALSO requests the full Live scope.

    Reuses the shared ``oauth_callback``: the state/session shape is the
    same as ``connect_platform``'s, so the callback exchanges the code and
    stores the wider grant on the same account. Only the authorize URL
    differs — ``include_live_scopes=True`` adds
    ``https://www.googleapis.com/auth/youtube`` to the requested scopes.
    After Google redirects back, the operator lands on the calendar; the
    Live monitor then re-checks the grant via tokeninfo and shows ready.
    """
    workspace = _get_workspace(request, workspace_id)
    account = get_object_or_404(
        SocialAccount,
        pk=account_id,
        workspace=workspace,
        platform="youtube",
    )
    provider = _get_provider_for_platform("youtube", workspace.organization_id)
    apply_analytics_scope_flag(provider, "youtube")
    provider.include_live_scopes = True
    nonce = secrets.token_urlsafe(32)
    state = _sign_state(workspace_id, "youtube", request.user.id, nonce)
    code_verifier = issue_pkce_verifier(provider)
    request.session[OAUTH_SESSION_KEY] = {
        "nonce": nonce,
        "workspace_id": str(workspace_id),
        "platform": "youtube",
        "code_verifier": code_verifier,
    }
    redirect_uri = _build_redirect_uri(request, "youtube")
    auth_url = provider.get_auth_url(redirect_uri, state, **pkce_kwargs(code_verifier))
    return redirect(auth_url)


# Keep the imported helper referenced: the masked key is rendered by the
# templates through ``LiveBroadcast.masked_stream_key``; this alias documents
# that the same masking is what the monitor shows.
__all__ = ["_is_insufficient_scope"]
