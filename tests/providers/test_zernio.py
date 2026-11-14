"""Tests for the Zernio provider.

No real Zernio call is ever made: ``_request`` is patched and the payload
/ status mapping is pinned against the shapes the working local upload
scripts use (``upload-zernio.py`` / ``upload-zernio-curl.sh``).
"""

from unittest.mock import MagicMock, patch

import pytest

from providers.exceptions import PublishError
from providers.types import PublishContent, PublishState, PublishStatus
from providers.zernio import (
    ZernioProvider,
    build_post_payload,
    map_post_status,
    mask_api_key,
    parse_accounts_response,
    parse_profiles_response,
    pick_default_profile,
    resolve_visibility,
)

SK_KEY = "sk_" + "a" * 64


def _response(payload):
    return MagicMock(json=MagicMock(return_value=payload), status_code=200)


def _provider(**credentials):
    return ZernioProvider(credentials=credentials)


class TestMaskApiKey:
    def test_masks_to_last_four(self):
        assert mask_api_key(SK_KEY) == "****aaaa"

    def test_empty_is_fully_masked(self):
        assert mask_api_key("") == "****"
        assert mask_api_key(None) == "****"


class TestDiscoveryParsing:
    def test_profiles_parsed_and_default_picked(self):
        profiles = parse_profiles_response(
            {
                "profiles": [
                    {"_id": "p1", "name": "One", "isDefault": False},
                    {"_id": "p2", "name": "Two", "isDefault": True},
                ]
            }
        )
        assert [p["id"] for p in profiles] == ["p1", "p2"]
        assert pick_default_profile(profiles)["id"] == "p2"

    def test_default_falls_back_to_first(self):
        assert pick_default_profile([{"id": "p1"}, {"id": "p2"}])["id"] == "p1"
        assert pick_default_profile([]) is None

    def test_accounts_parsed(self):
        accounts = parse_accounts_response(
            {
                "accounts": [
                    {
                        "_id": "acc1",
                        "platform": "YouTube",
                        "username": "beritakenapa",
                        "displayName": "BeritaKenapa",
                        "isActive": True,
                    },
                    {"_id": "acc2", "platform": "tiktok", "username": "bk", "isActive": False},
                ]
            },
            profile_id="prof1",
        )
        assert accounts[0] == {
            "account_id": "acc1",
            "platform": "youtube",
            "username": "beritakenapa",
            "display_name": "BeritaKenapa",
            "is_active": True,
            "profile_id": "prof1",
        }
        assert accounts[1]["is_active"] is False


class TestPostStatusMapping:
    def test_terminal_states(self):
        assert map_post_status("published") == PublishState.COMPLETE
        assert map_post_status("failed") == PublishState.FAILED
        assert map_post_status("rejected") == PublishState.FAILED

    def test_in_flight_states(self):
        for status in ("scheduled", "publishing", "processing", "queued", "draft"):
            assert map_post_status(status) == PublishState.PENDING

    def test_unknown_is_pending_not_failed(self):
        assert map_post_status("weird-new-status") == PublishState.PENDING
        assert map_post_status(None) == PublishState.PENDING


class TestBuildPostPayload:
    def _content(self, **kwargs):
        defaults = {
            "text": "Rupiah melemah ke Rp18.115",
            "title": "Rupiah Melemah",
            "media_urls": ["https://media.zernio.com/temp/video.mp4"],
            "media_types": ["video"],
        }
        defaults.update(kwargs)
        return PublishContent(**defaults)

    def test_video_payload_matches_local_script(self):
        # Explicit "public": the bare default is now private (F4), so the
        # public case has to be asked for, not assumed.
        payload = build_post_payload(
            content=self._content(extra={"visibility": "public"}),
            profile_id="prof1",
            destination_platform="youtube",
            account_id="acc-yt",
        )
        assert payload["content"] == "Rupiah melemah ke Rp18.115"
        assert payload["title"] == "Rupiah Melemah"
        assert payload["visibility"] == "public"
        assert payload["profileId"] == "prof1"
        assert payload["platforms"] == [{"platform": "youtube", "accountId": "acc-yt"}]
        assert payload["mediaItems"] == [{"type": "video", "url": "https://media.zernio.com/temp/video.mp4"}]
        assert payload["publishNow"] is True
        assert "tiktokSettings" not in payload

    def test_tiktok_gets_settings_block(self):
        payload = build_post_payload(
            content=self._content(),
            profile_id="prof1",
            destination_platform="tiktok",
            account_id="acc-tt",
        )
        assert payload["platforms"] == [{"platform": "tiktok", "accountId": "acc-tt"}]
        assert payload["tiktokSettings"]["privacy_level"] == "PUBLIC_TO_EVERYONE"
        assert payload["tiktokSettings"]["video_made_with_ai"] is False

    def test_image_media_type_and_tags(self):
        payload = build_post_payload(
            content=self._content(
                media_urls=["https://example.com/a.jpg"],
                media_types=["image"],
                extra={"tags": ["berita", " rupiah "]},
            ),
            profile_id="prof1",
            destination_platform="instagram",
            account_id="acc-ig",
        )
        assert payload["mediaItems"] == [{"type": "image", "url": "https://example.com/a.jpg"}]
        assert payload["tags"] == ["berita", "rupiah"]

    def test_title_defaults_to_text_and_truncates_to_100(self):
        payload = build_post_payload(
            content=self._content(title="", text="x" * 150, media_urls=[]),
            profile_id="prof1",
            destination_platform="youtube",
            account_id="acc-yt",
        )
        assert payload["title"] == "x" * 100
        assert payload["mediaItems"] == []

    def test_draft_flag(self):
        payload = build_post_payload(
            content=self._content(extra={"is_draft": True}),
            profile_id="prof1",
            destination_platform="youtube",
            account_id="acc-yt",
        )
        assert payload["isDraft"] is True
        assert "publishNow" not in payload

    def test_scheduled_for_flag(self):
        # F12 read side: platform_extra["scheduled_for"] (ISO string) ->
        # scheduledFor, and no publishNow.
        payload = build_post_payload(
            content=self._content(extra={"scheduled_for": "2026-11-15T10:00:00+07:00"}),
            profile_id="prof1",
            destination_platform="tiktok",
            account_id="acc-tt",
        )
        assert payload["scheduledFor"] == "2026-11-15T10:00:00+07:00"
        assert "publishNow" not in payload
        assert "isDraft" not in payload

    def test_draft_wins_over_scheduled_for(self):
        payload = build_post_payload(
            content=self._content(extra={"is_draft": True, "scheduled_for": "2026-11-15T10:00:00+07:00"}),
            profile_id="prof1",
            destination_platform="youtube",
            account_id="acc-yt",
        )
        assert payload["isDraft"] is True
        assert "scheduledFor" not in payload
        assert "publishNow" not in payload

    def test_is_draft_false_does_not_draft(self):
        payload = build_post_payload(
            content=self._content(extra={"is_draft": False}),
            profile_id="prof1",
            destination_platform="youtube",
            account_id="acc-yt",
        )
        assert "isDraft" not in payload
        assert payload["publishNow"] is True


class TestPublishPost:
    def _bound_provider(self):
        return _provider(
            api_key=SK_KEY,
            account_id="acc-yt",
            profile_id="prof1",
            destination_platform="youtube",
        )

    @patch.object(ZernioProvider, "_request")
    def test_publish_returns_zernio_id(self, mock_request):
        mock_request.return_value = _response({"_id": "post1234567890abcdefgh", "status": "publishing"})
        result = self._bound_provider().publish_post(
            None,
            PublishContent(text="hi", title="Hi", media_urls=["https://example.com/v.mp4"], media_types=["video"]),
        )
        assert result.platform_post_id == "post1234567890abcdefgh"
        assert result.extra["zernio_status"] == "publishing"
        method, url = mock_request.call_args.args[0], mock_request.call_args.args[1]
        assert method == "POST"
        assert url == "https://zernio.com/api/v1/posts"
        sent = mock_request.call_args.kwargs["json"]
        assert sent["platforms"] == [{"platform": "youtube", "accountId": "acc-yt"}]
        assert sent["publishNow"] is True

    @patch.object(ZernioProvider, "_request")
    def test_youtube_via_zernio_is_not_blocked(self, mock_request):
        # Routing follows the account: a Zernio-bound YouTube account
        # publishes through Zernio, alongside direct YouTube OAuth
        # accounts — no destination-based refusal exists on this path.
        mock_request.return_value = _response({"_id": "post-yt-1", "status": "publishing"})
        result = self._bound_provider().publish_post(
            None,
            PublishContent(text="hi", title="Hi", media_urls=["https://example.com/v.mp4"], media_types=["video"]),
        )
        assert result.platform_post_id == "post-yt-1"
        sent = mock_request.call_args.kwargs["json"]
        assert sent["platforms"] == [{"platform": "youtube", "accountId": "acc-yt"}]

    @patch.object(ZernioProvider, "_request")
    def test_publish_uses_access_token_as_key_when_no_credentials(self, mock_request):
        mock_request.return_value = _response({"_id": "post1", "status": "scheduled"})
        provider = _provider(account_id="acc-tt", profile_id="prof1", destination_platform="tiktok")
        provider.publish_post(SK_KEY, PublishContent(text="hi", media_urls=[]))
        assert mock_request.call_args.kwargs["access_token"] == SK_KEY

    def test_publish_without_key_refuses(self):
        with pytest.raises(PublishError) as excinfo:
            _provider(account_id="acc", profile_id="p", destination_platform="youtube").publish_post(
                None, PublishContent(text="hi")
            )
        assert excinfo.value.retryable is False

    def test_publish_without_binding_refuses(self):
        with pytest.raises(PublishError):
            _provider(api_key=SK_KEY).publish_post(None, PublishContent(text="hi"))

    def test_publish_unknown_destination_refuses(self):
        with pytest.raises(PublishError):
            _provider(api_key=SK_KEY, account_id="a", profile_id="p", destination_platform="whatsapp").publish_post(
                None, PublishContent(text="hi")
            )


class TestCheckPublishStatus:
    @patch.object(ZernioProvider, "_request")
    def test_published_is_complete(self, mock_request):
        mock_request.return_value = _response({"status": "published", "url": "https://youtube.com/watch?v=x"})
        status = _provider(api_key=SK_KEY).check_publish_status(SK_KEY, "post1")
        assert isinstance(status, PublishStatus)
        assert status.state is PublishState.COMPLETE
        assert status.platform_post_id == "post1"

    @patch.object(ZernioProvider, "_request")
    def test_publishing_is_pending(self, mock_request):
        mock_request.return_value = _response({"status": "publishing"})
        status = _provider(api_key=SK_KEY).check_publish_status(SK_KEY, "post1")
        assert status.state is PublishState.PENDING
        assert status.platform_post_id == ""

    @patch.object(ZernioProvider, "_request")
    def test_failed_carries_error(self, mock_request):
        mock_request.return_value = _response({"status": "failed", "error": "TikTok rejected the video."})
        status = _provider(api_key=SK_KEY).check_publish_status(SK_KEY, "post1")
        assert status.state is PublishState.FAILED
        assert "TikTok rejected" in status.error


class TestDiscoveryCalls:
    @patch.object(ZernioProvider, "_request")
    def test_list_accounts_uses_default_profile(self, mock_request):
        mock_request.side_effect = [
            _response({"profiles": [{"_id": "p1", "name": "A", "isDefault": True}]}),
            _response({"accounts": [{"_id": "a1", "platform": "youtube", "username": "bk", "isActive": True}]}),
        ]
        accounts = _provider().list_accounts(SK_KEY)
        assert len(accounts) == 1
        assert accounts[0]["account_id"] == "a1"
        assert accounts[0]["profile_id"] == "p1"
        second_call = mock_request.call_args_list[1]
        assert second_call.kwargs["params"] == {"profileId": "p1"}

    @patch.object(ZernioProvider, "_request")
    def test_list_accounts_no_profiles_errors(self, mock_request):
        mock_request.return_value = _response({"profiles": []})
        with pytest.raises(Exception, match="no profiles"):
            _provider().list_accounts(SK_KEY)

    @patch.object(ZernioProvider, "_request")
    def test_validate_token(self, mock_request):
        from providers.exceptions import APIError

        mock_request.return_value = _response({"profiles": [{"_id": "p1"}]})
        assert _provider().validate_token(SK_KEY) is True
        mock_request.side_effect = APIError("bad key", status_code=401)
        assert _provider().validate_token(SK_KEY) is False


class TestBuildPostPayloadParity:
    """Composer panel options survive the trip to POST /v1/posts.

    Pins the mapping the composer parity work relies on: YouTube
    privacy_status -> visibility (unlisted -> private, lossy by design),
    TikTok panel -> tiktokSettings, and dead options never sent.
    """

    def _content(self, **kwargs):
        defaults = {
            "text": "Rupiah melemah ke Rp18.115",
            "title": "Rupiah Melemah",
            "media_urls": ["https://media.zernio.com/temp/video.mp4"],
            "media_types": ["video"],
        }
        defaults.update(kwargs)
        return PublishContent(**defaults)

    def _payload(self, destination, **extra):
        return build_post_payload(
            content=self._content(extra=extra),
            profile_id="prof1",
            destination_platform=destination,
            account_id="acc-1",
        )

    def test_youtube_privacy_status_becomes_visibility(self):
        payload = self._payload("youtube", privacy_status="private", tags=["berita"])
        assert payload["visibility"] == "private"
        assert payload["tags"] == ["berita"]

    def test_unlisted_maps_to_private(self):
        # Zernio has no "unlisted": the safe, lossy mapping is private —
        # never more public than the author chose.
        assert resolve_visibility({"privacy_status": "unlisted"}) == "private"
        assert self._payload("youtube", privacy_status="unlisted")["visibility"] == "private"

    def test_explicit_visibility_wins_over_privacy_status(self):
        payload = self._payload("youtube", visibility="private", privacy_status="public")
        assert payload["visibility"] == "private"

    def test_unknown_visibility_defaults_to_private(self):
        # Fail-closed (F4): an unreadable visibility must never make a
        # restricted video public — same principle as unlisted -> private.
        assert resolve_visibility({"privacy_status": "weird"}) == "private"
        assert resolve_visibility({}) == "private"
        assert resolve_visibility(None) == "private"

    def test_tiktok_panel_maps_to_settings(self):
        payload = self._payload(
            "tiktok",
            privacy_level="MUTUAL_FOLLOW_FRIENDS",
            disable_comment=True,
            disable_duet=False,
            disable_stitch=True,
            is_aigc=True,
        )
        settings = payload["tiktokSettings"]
        assert settings["privacy_level"] == "MUTUAL_FOLLOW_FRIENDS"
        assert settings["allow_comment"] is False
        assert settings["allow_duet"] is True
        assert settings["allow_stitch"] is False
        assert settings["video_made_with_ai"] is True
        # Untouched defaults from the working local script.
        assert settings["commercial_content_type"] == "none"
        assert settings["media_type"] == "video"

    def test_tiktok_unknown_privacy_level_keeps_default(self):
        payload = self._payload("tiktok", privacy_level="EVERYONE_ELSE")
        assert payload["tiktokSettings"]["privacy_level"] == "PUBLIC_TO_EVERYONE"

    def test_tiktok_defaults_allow_everything(self):
        settings = self._payload("tiktok")["tiktokSettings"]
        assert settings["allow_comment"] is True
        assert settings["allow_duet"] is True
        assert settings["allow_stitch"] is True
        assert settings["video_made_with_ai"] is False

    def test_dead_options_are_never_sent(self):
        payload = self._payload(
            "youtube",
            privacy_status="public",
            self_declared_made_for_kids=True,
            thumbnail_file="/tmp/thumb.jpg",
            thumbnail_asset_id="x",
            brand_content_toggle=True,
            video_cover_timestamp_ms=1500,
            board_id="board123",
            link_url="https://example.com",
            post_type="reel",
        )
        assert "self_declared_made_for_kids" not in payload
        assert "thumbnail_file" not in payload
        assert "thumbnail_asset_id" not in payload
        assert "brand_content_toggle" not in payload
        assert "video_cover_timestamp_ms" not in payload
        assert "board_id" not in payload
        assert "link_url" not in payload
        assert "post_type" not in payload
        assert "tiktokSettings" not in payload
