# Hide Zernio from the OAuth connect grid: Zernio accounts connect
# through the key pool (workspace/<id>/zernio/), not through OAuth, so a
# greyed-out Zernio card with a "credentials not configured" overlay would
# only mislead. Reversible: delete the row to show it again.

from django.db import migrations


def hide_zernio(apps, schema_editor):
    PlatformVisibility = apps.get_model("social_accounts", "PlatformVisibility")
    PlatformVisibility.objects.get_or_create(platform="zernio", defaults={"is_visible": False})


def show_zernio(apps, schema_editor):
    PlatformVisibility = apps.get_model("social_accounts", "PlatformVisibility")
    PlatformVisibility.objects.filter(platform="zernio").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("zernio", "0001_initial"),
        ("social_accounts", "0026_livebroadcast"),
    ]

    operations = [
        migrations.RunPython(hide_zernio, show_zernio),
    ]
