# Identify a Zernio key by its owner's login email (``email|apikey`` format).
# Safe for existing rows: the field is blank by default and the UI falls
# back to ``label`` when no email is stored. The email is deliberately not
# unique — one Zernio login can own several API keys.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("zernio", "0002_hide_from_connect"),
    ]

    operations = [
        migrations.AddField(
            model_name="zernioapikey",
            name="owner_email",
            field=models.EmailField(
                blank=True,
                default="",
                help_text="Zernio login email of the key owner — display identity only, not verified against Zernio.",
                max_length=254,
            ),
        ),
    ]
