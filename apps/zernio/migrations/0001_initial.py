# Generated for the Zernio key-pool integration (local patch, not upstream).

import apps.common.encryption
import django.db.models.deletion
import uuid
from django.db import migrations, models
from django.utils import timezone


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("social_accounts", "0026_livebroadcast"),
        ("workspaces", "0003_alter_workspace_primary_color_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="ZernioApiKey",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("label", models.CharField(help_text="e.g. BeritaKenapa key 1", max_length=100)),
                (
                    "api_key",
                    apps.common.encryption.EncryptedTextField(
                        help_text="Zernio API key (sk_ + 64 hex). Shown once, then never again."
                    ),
                ),
                (
                    "key_suffix",
                    models.CharField(editable=False, help_text="Last 4 characters, for display only.", max_length=4),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[("active", "Active"), ("full", "Full — 2/2 accounts"), ("disabled", "Disabled")],
                        default="active",
                        max_length=20,
                    ),
                ),
                ("last_tested_at", models.DateTimeField(blank=True, null=True)),
                ("last_error", models.CharField(blank=True, default="", max_length=500)),
                ("created_at", models.DateTimeField(default=timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "workspace",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="zernio_api_keys",
                        to="workspaces.workspace",
                    ),
                ),
            ],
            options={
                "db_table": "zernio_api_key",
                "ordering": ["created_at"],
            },
        ),
        migrations.CreateModel(
            name="ZernioAccountBinding",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("zernio_account_id", models.CharField(help_text="Zernio account _id.", max_length=255)),
                ("zernio_profile_id", models.CharField(help_text="Zernio profile _id.", max_length=255)),
                (
                    "destination_platform",
                    models.CharField(
                        choices=[
                            ("youtube", "YouTube"),
                            ("tiktok", "TikTok"),
                            ("facebook", "Facebook"),
                            ("instagram", "Instagram"),
                            ("threads", "Threads"),
                            ("pinterest", "Pinterest"),
                            ("bluesky", "Bluesky"),
                            ("google_business", "Google Business"),
                            ("linkedin", "LinkedIn"),
                        ],
                        help_text="Zernio platform slug for this account (youtube, tiktok, ...).",
                        max_length=30,
                    ),
                ),
                ("created_at", models.DateTimeField(default=timezone.now)),
                (
                    "api_key",
                    models.ForeignKey(
                        help_text="Deleting a key with bound accounts is blocked; disable it instead.",
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="bindings",
                        to="zernio.zernioapikey",
                    ),
                ),
                (
                    "social_account",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="zernio_binding",
                        to="social_accounts.socialaccount",
                    ),
                ),
            ],
            options={
                "db_table": "zernio_account_binding",
            },
        ),
    ]
