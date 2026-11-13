"""YouTube Live monitor URLs, mounted at ``workspace/<id>/live/``."""

from django.urls import path

from . import live_views

app_name = "live"

urlpatterns = [
    path("", live_views.live_dashboard, name="dashboard"),
    path("create/", live_views.live_create, name="create"),
    path("created/<uuid:broadcast_id>/", live_views.live_created, name="created"),
    path("<uuid:broadcast_id>/transition/", live_views.live_transition, name="transition"),
    path("<uuid:broadcast_id>/delete/", live_views.live_delete, name="delete"),
    path("<uuid:broadcast_id>/recreate-stream/", live_views.live_recreate_stream, name="recreate_stream"),
    path("<uuid:broadcast_id>/reveal-key/", live_views.live_reveal_key, name="reveal_key"),
    path("status/", live_views.live_status, name="status"),
    path("reconnect/<uuid:account_id>/", live_views.live_reconnect, name="reconnect"),
]
