from __future__ import annotations

import time
from datetime import datetime

from django import forms


class SessionForm(forms.Form):
    kind = forms.ChoiceField(
        choices=(("pomodoro", "Pomodoro"), ("break", "Break")),
    )
    date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    start_time = forms.TimeField(widget=forms.TimeInput(attrs={"type": "time"}))
    duration_minutes = forms.IntegerField(
        min_value=1,
        max_value=24 * 60,
        widget=forms.NumberInput(attrs={"min": 1, "max": 24 * 60}),
    )
    name = forms.CharField(required=False, max_length=200)
    project = forms.CharField(
        required=False,
        max_length=200,
        widget=forms.TextInput(attrs={"list": "project-options"}),
    )

    def __init__(self, *args, active: bool = False, session: dict | None = None, **kwargs):
        self.active = active
        self.session = session
        super().__init__(*args, **kwargs)
        if active:
            for field_name in ("kind", "date", "start_time", "duration_minutes"):
                self.fields[field_name].disabled = True
            self.fields["kind"].help_text = "Timing is locked while this session is active."
            self.fields["date"].help_text = ""
            self.fields["start_time"].help_text = ""
            self.fields["duration_minutes"].help_text = ""

    def clean(self):
        cleaned = super().clean()
        if self.active:
            return cleaned
        date_value = cleaned.get("date")
        time_value = cleaned.get("start_time")
        if date_value is not None and time_value is not None:
            start_epoch = int(datetime.combine(date_value, time_value).timestamp())
            if start_epoch > time.time() + 60:
                raise forms.ValidationError("Start time cannot be in the future.")
        return cleaned

    def session_fields(self) -> dict:
        date_value = self.cleaned_data["date"]
        time_value = self.cleaned_data["start_time"]
        duration = int(self.cleaned_data["duration_minutes"]) * 60
        start_epoch = int(datetime.combine(date_value, time_value).timestamp())
        name = (self.cleaned_data.get("name") or "").strip() or None
        project = (self.cleaned_data.get("project") or "").strip() or None
        return {
            "kind": self.cleaned_data["kind"],
            "start_epoch": start_epoch,
            "duration": duration,
            "ended_at": start_epoch + duration,
            "name": name,
            "project": project,
        }


def initial_for_session(session: dict) -> dict:
    start = datetime.fromtimestamp(int(session["start_epoch"]))
    return {
        "kind": session.get("kind") or "pomodoro",
        "date": start.date(),
        "start_time": start.time().replace(second=0, microsecond=0),
        "duration_minutes": max(1, int(session["duration"]) // 60),
        "name": session.get("name") or "",
        "project": session.get("project") or "",
    }


def initial_for_new() -> dict:
    now = datetime.now()
    return {
        "kind": "pomodoro",
        "date": now.date(),
        "start_time": now.time().replace(second=0, microsecond=0),
        "duration_minutes": 25,
        "name": "",
        "project": "",
    }
