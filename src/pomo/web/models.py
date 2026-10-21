from __future__ import annotations

from django.db import models


class Session(models.Model):
    """A pomodoro/break session, matching the legacy ``sessions`` table."""

    id = models.CharField(max_length=36, primary_key=True)
    state = models.CharField(max_length=32)
    start_epoch = models.IntegerField()
    duration = models.IntegerField()
    origin_machine = models.CharField(max_length=255)
    updated_at = models.FloatField()
    ended_at = models.FloatField(null=True, blank=True)
    name = models.TextField(null=True, blank=True)
    project = models.TextField(null=True, blank=True)
    kind = models.CharField(max_length=16, null=True, blank=True)

    class Meta:
        db_table = "sessions"
        indexes = [
            models.Index(fields=["start_epoch"], name="sessions_start_epoch_idx"),
            models.Index(fields=["project"], name="sessions_project_idx"),
            models.Index(fields=["state"], name="sessions_state_idx"),
            models.Index(fields=["kind", "state"], name="sessions_kind_state_idx"),
        ]

    def __str__(self) -> str:
        return f"{str(self.id)[:8]} ({self.state})"


class Current(models.Model):
    """Singleton row pointing at the session agents should adopt."""

    singleton = models.IntegerField(primary_key=True)
    session_id = models.CharField(max_length=36, null=True, blank=True)
    updated_at = models.FloatField(null=True, blank=True)

    class Meta:
        db_table = "current"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(singleton=0),
                name="current_singleton_zero",
            )
        ]

    def __str__(self) -> str:
        return f"current -> {self.session_id or 'none'}"
