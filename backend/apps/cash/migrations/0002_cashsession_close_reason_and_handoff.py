# Generated manually for one-shift-per-day + cashier handoff.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("cash", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="cashsession",
            name="close_reason",
            field=models.CharField(
                blank=True,
                choices=[("NORMAL", "Normal close"), ("TRANSFER", "Handed off to another cashier")],
                default="",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="cashsession",
            name="previous_session",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="successor_sessions",
                to="cash.cashsession",
            ),
        ),
        migrations.AddField(
            model_name="cashsession",
            name="superseded_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="supersedes",
                to="cash.cashsession",
            ),
        ),
    ]
