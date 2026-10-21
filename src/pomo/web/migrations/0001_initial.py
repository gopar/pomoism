from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Current",
            fields=[
                ("singleton", models.IntegerField(primary_key=True, serialize=False)),
                ("session_id", models.CharField(blank=True, max_length=36, null=True)),
                ("updated_at", models.FloatField(blank=True, null=True)),
            ],
            options={
                "db_table": "current",
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("singleton", 0)),
                        name="current_singleton_zero",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="Session",
            fields=[
                ("id", models.CharField(max_length=36, primary_key=True, serialize=False)),
                ("state", models.CharField(max_length=32)),
                ("start_epoch", models.IntegerField()),
                ("duration", models.IntegerField()),
                ("origin_machine", models.CharField(max_length=255)),
                ("updated_at", models.FloatField()),
                ("ended_at", models.FloatField(blank=True, null=True)),
                ("name", models.TextField(blank=True, null=True)),
                ("project", models.TextField(blank=True, null=True)),
                ("kind", models.CharField(blank=True, max_length=16, null=True)),
            ],
            options={
                "db_table": "sessions",
            },
        ),
    ]
