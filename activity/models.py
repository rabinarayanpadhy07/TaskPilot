from django.conf import settings
from django.db import models
from django.db.models import Q


class Activity(models.Model):
    """
    Issue history. Written server-side whenever an issue changes (see
    activity/services.py); clients can only read it.
    """

    class Kind(models.TextChoices):
        CREATED = "created", "Created"
        UPDATED = "updated", "Updated"
        COMMENTED = "commented", "Commented"
        ATTACHMENT_ADDED = "attachment_added", "Attachment added"

    # Denormalized so the workspace feed is one indexed query
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="+")
    issue = models.ForeignKey("issues.Issue", on_delete=models.CASCADE, related_name="activity")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    field = models.CharField(max_length=40, blank=True)
    from_value = models.JSONField(null=True, blank=True)
    to_value = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=False)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(fields=["issue", "created_at"]),
            models.Index(fields=["workspace", "-created_at"]),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(kind__in=["created", "updated", "commented", "attachment_added"]), name="activity_kind_valid"),
        ]
