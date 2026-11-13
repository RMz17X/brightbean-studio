from django.contrib import admin

from .models import ZernioAccountBinding, ZernioApiKey


@admin.register(ZernioApiKey)
class ZernioApiKeyAdmin(admin.ModelAdmin):
    list_display = ("display_name", "workspace", "masked_key", "status", "slots_used", "created_at")
    list_filter = ("status",)
    search_fields = ("owner_email", "label", "key_suffix")
    readonly_fields = ("key_suffix", "masked_key", "created_at", "updated_at")


@admin.register(ZernioAccountBinding)
class ZernioAccountBindingAdmin(admin.ModelAdmin):
    list_display = ("social_account", "api_key", "destination_platform", "zernio_account_id", "created_at")
    list_filter = ("destination_platform",)
    search_fields = ("social_account__account_name", "zernio_account_id")
