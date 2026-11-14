"""``GET /api/v1/accounts`` — list the connected accounts this key may target.

``PATCH /api/v1/accounts/{id}/profile`` — update one account's public
profile (YouTube description / Facebook Page info) on the platform.
"""

from __future__ import annotations

import uuid

from django.http import HttpRequest
from ninja import Query, Router
from ninja.errors import HttpError

from apps.api.limits import enforce_http_rate_limits
from apps.api.middleware import log_audit_entry
from apps.api.pagination import decode_offset_cursor, encode_offset_cursor
from apps.api.schemas import (
    AccountProfileResponse,
    AccountProfileUpdateRequest,
    AccountsListResponse,
    AccountSummary,
)
from apps.social_accounts import profile_service
from apps.social_accounts.models import SocialAccount
from apps.social_accounts.profile_service import ProfileServiceError

router = Router(tags=["accounts"])


def _require_perm(request: HttpRequest, key: str) -> None:
    """Re-check a workspace permission inside a Ninja route body.

    Same inline check as the live/posts routers: Ninja routes can't
    use ``@require_permission`` (it expects a Django view signature).
    """
    membership = getattr(request, "workspace_membership", None)
    if membership is None or not membership.effective_permissions.get(key, False):
        raise HttpError(403, f"Permission denied: {key}")


def _resolve_allowed_account(request: HttpRequest, account_id: uuid.UUID) -> SocialAccount:
    """Resolve the target account and verify it is in the key's allowlist.

    Same confused-deputy defence as the live router: even with a valid
    bearer, the caller may only act on accounts the issuer explicitly
    listed at issuance time. An account outside the allowlist (including
    one in another workspace) answers 403, never 404-with-a-hint.
    """
    api_key = request.api_key  # type: ignore[attr-defined]
    allowlist_ids = {sa.id for sa in api_key.social_accounts.all()}
    if account_id not in allowlist_ids:
        raise HttpError(403, "SocialAccount is not in this key's allowlist.")
    return SocialAccount.objects.get(id=account_id)


@router.get(
    "/",
    response=AccountsListResponse,
    summary="List the SocialAccounts this API key is allowed to act on",
    description=(
        "Cursor pagination like every other list endpoint: `limit` (1-100, "
        "default 50) and the `next_cursor` from the previous page. The list "
        "is bounded by the key's allowlist, so a single page normally holds "
        "all of them."
    ),
)
def list_accounts(request, limit: int = Query(50, ge=1, le=100), cursor: str | None = Query(None)):
    enforce_http_rate_limits(request, is_write=False)
    try:
        offset = decode_offset_cursor(cursor)
    except ValueError as exc:
        raise HttpError(422, "cursor is not a valid pagination cursor") from exc
    api_key = request.api_key
    # Explicit order (name + id tiebreak) so cursor pages are stable.
    all_accounts = list(
        api_key.social_accounts.order_by("account_name", "id").prefetch_related("zernio_binding").all()
    )
    page = all_accounts[offset : offset + limit + 1]
    has_more = len(page) > limit
    page = page[:limit]
    accounts = [AccountSummary.from_social_account(sa) for sa in page]
    log_audit_entry(request, action="accounts.list", target_id=None, status_code=200)
    return AccountsListResponse(
        accounts=accounts,
        limit=limit,
        next_cursor=encode_offset_cursor(offset + limit) if has_more else None,
    )


@router.patch(
    "/{account_id}/profile",
    response=AccountProfileResponse,
    summary="Update an account's public profile (YouTube description / Facebook Page info)",
)
def update_account_profile(request, account_id: uuid.UUID, payload: AccountProfileUpdateRequest):
    enforce_http_rate_limits(request, is_write=True)
    _require_perm(request, "create_posts")
    account = _resolve_allowed_account(request, account_id)
    # At least one non-blank field: the provider refuses an empty
    # update anyway, but catching it here keeps the 422 on our side
    # and never spends a platform call on a no-op.
    given = {
        name: value
        for name, value in payload.model_dump().items()
        if value is not None and str(value).strip()
    }
    if not given:
        raise HttpError(
            422,
            "Send at least one field to update: description (YouTube), or "
            "about / description / website / phone (Facebook Page).",
        )
    try:
        updated = profile_service.update_account_profile(
            account,
            description=payload.description,
            about=payload.about,
            website=payload.website,
            phone=payload.phone,
        )
    except ProfileServiceError as exc:
        raise HttpError(exc.status_code, exc.message) from exc
    log_audit_entry(request, action="accounts.update_profile", target_id=account.id, status_code=200)
    return AccountProfileResponse(
        id=account.id,
        platform=updated["platform"],
        transport=updated.get("transport"),
        description=updated.get("description"),
        about=updated.get("about"),
        website=updated.get("website"),
        phone=updated.get("phone"),
        updated_fields=updated.get("updated_fields", []),
    )
