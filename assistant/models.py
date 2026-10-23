"""Data model. The Site record (with its notes and releases) is the assistant's memory."""

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from . import crypto


class LiaraConnection(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active"
        INVALID = "invalid"

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="liara")
    encrypted_token = models.TextField()
    team_id = models.CharField(max_length=100, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    last_verified_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)

    def set_token(self, token: str):
        self.encrypted_token = crypto.encrypt(token)

    def get_token(self) -> str:
        return crypto.decrypt(self.encrypted_token)

    def __str__(self):
        return f"LiaraConnection({self.user_id}, {self.status})"


class Site(models.Model):
    class Status(models.TextChoices):
        DEPLOYING = "deploying"
        LIVE = "live"
        FAILED = "failed"
        UNKNOWN = "unknown"
        GONE = "gone"  # deleted in the Liara console

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sites")
    app_name = models.CharField(max_length=100)
    platform = models.CharField(max_length=40, blank=True)
    plan_id = models.CharField(max_length=60, blank=True)
    url = models.URLField(blank=True)
    created_by_assistant = models.BooleanField(default=False)
    db_name = models.CharField(max_length=100, blank=True)
    db_type = models.CharField(max_length=40, blank=True)
    bucket_name = models.CharField(max_length=100, blank=True)
    # {"visitors": "100-1000", "purpose": "business"}
    answers = models.JSONField(default=dict, blank=True)
    # {"ram_percent": .., "cpu_percent": .., "response_ms": .., "taken_at": iso}
    baseline = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.UNKNOWN)
    deployed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("user", "app_name")]
        ordering = ["-deployed_at", "-created_at"]

    def __str__(self):
        return self.app_name


class Release(models.Model):
    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="releases")
    release_id = models.CharField(max_length=100)
    source_id = models.CharField(max_length=200, blank=True)
    state = models.CharField(max_length=30, blank=True)
    fail_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = [("site", "release_id")]
        ordering = ["-created_at"]


class SiteNote(models.Model):
    class Kind(models.TextChoices):
        DEPLOY = "deploy"
        INCIDENT = "incident"
        FIX = "fix"
        DECISION = "decision"

    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="notes")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class Conversation(models.Model):
    """One ongoing thread per user. `state` holds the scripted-flow position."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="conversation")
    # e.g. {"flow": "deploy", "step": "visitors", "upload_id": "...", "inspection": {...}}
    state = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class Message(models.Model):
    class Role(models.TextChoices):
        USER = "user"
        BOT = "bot"

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=10, choices=Role.choices)
    text = models.TextField(blank=True)
    # List of UI blocks: [{"type": "buttons", ...}, {"type": "card", ...}, ...]
    ui = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]


class PendingAction(models.Model):
    class Status(models.TextChoices):
        PROPOSED = "proposed"
        APPROVED = "approved"
        REJECTED = "rejected"
        EXPIRED = "expired"
        RUNNING = "running"
        SUCCEEDED = "succeeded"
        FAILED = "failed"
        UNKNOWN = "unknown"

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="actions")
    site = models.ForeignKey(Site, null=True, blank=True, on_delete=models.SET_NULL, related_name="actions")
    tool = models.CharField(max_length=60)
    args = models.JSONField(default=dict)
    summary = models.JSONField(default=list)  # lines shown on the approval card
    cost_text = models.CharField(max_length=200, blank=True)
    downtime_text = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PROPOSED)
    idempotency_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    # sha256 of the canonical args at proposal time; execution refuses if they changed.
    args_digest = models.CharField(max_length=64, blank=True)
    message = models.ForeignKey(Message, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    @property
    def is_expired(self):
        return timezone.now() >= self.expires_at


class Job(models.Model):
    pending_action = models.OneToOneField(PendingAction, on_delete=models.CASCADE, related_name="job")
    # [{"label": "...", "state": "waiting|active|done|failed"}]
    steps = models.JSONField(default=list)
    current_step = models.IntegerField(default=0)
    error = models.TextField(blank=True)
    result = models.JSONField(default=dict, blank=True)
    message = models.ForeignKey(Message, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)


class AuditLog(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    site = models.ForeignKey(Site, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    tool = models.CharField(max_length=60)
    args = models.JSONField(default=dict)  # redacted
    result = models.TextField(blank=True)  # redacted
    risk = models.CharField(max_length=20, default="change")
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)


class LLMUsage(models.Model):
    """Per-user daily LLM call counter for the daily cap."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    day = models.DateField()
    calls = models.IntegerField(default=0)

    class Meta:
        unique_together = [("user", "day")]


class HeldSecret(models.Model):
    """A value the user typed for an env var (e.g. a third-party API key).

    Held encrypted only until the approved job writes it to Liara, then deleted:
    on success, failure, rejection, cancel or expiry. Never shown, never in a
    Message, never sent to the LLM, never in the audit log (names only)."""

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="held_secrets")
    action = models.ForeignKey(PendingAction, null=True, blank=True, on_delete=models.CASCADE, related_name="held_secrets")
    name = models.CharField(max_length=100)
    encrypted_value = models.TextField()
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
