"""Zernio key-pool URLs, mounted at ``workspace/<id>/zernio/``."""

from django.urls import path

from . import views

app_name = "zernio"

urlpatterns = [
    path("", views.zernio_dashboard, name="dashboard"),
    path("keys/add/", views.add_key, name="add_key"),
    path("keys/<uuid:key_id>/edit/", views.edit_key, name="edit_key"),
    path("keys/<uuid:key_id>/test/", views.test_key, name="test_key"),
    path("keys/<uuid:key_id>/discover/", views.discover, name="discover"),
    path("keys/<uuid:key_id>/disable/", views.set_key_disabled, name="set_key_disabled"),
    path("keys/<uuid:key_id>/delete/", views.delete_key, name="delete_key"),
    path("accounts/connect/", views.connect_account, name="connect_account"),
    path("bindings/<uuid:binding_id>/unbind/", views.unbind_account, name="unbind_account"),
    path("bindings/<uuid:binding_id>/rebind/", views.rebind_account, name="rebind_account"),
]
