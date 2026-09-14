# Generated manually to match TenantScopedModel + WebPushSubscription fields.
import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("organizations", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="WebPushSubscription",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("endpoint", models.URLField(max_length=500)),
                ("p256dh", models.CharField(max_length=200)),
                ("auth", models.CharField(max_length=100)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="%(app_label)s_%(class)s_set",
                        to="organizations.organization",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="web_push_subscriptions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "web_push_subscriptions",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="webpushsubscription",
            index=models.Index(
                fields=["organization", "user"], name="web_push_su_organiz_7c0b1a_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="webpushsubscription",
            constraint=models.UniqueConstraint(
                fields=("endpoint",), name="uq_web_push_endpoint"
            ),
        ),
    ]
