from django.conf import settings
from django.db import models
from django.db.models import Q


class Notification(models.Model):
    """Created server-side when something happens that a person should know about."""

    class Category(models.TextChoices):
        ASSIGNMENT = "assignment", "Assignment"
        MENTION = "mention", "Mention"
        COMMENT = "comment", "Comment"
        UPDATE = "update", "Update"

    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="+")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    category = models.CharField(max_length=20, choices=Category.choices)
    summary = models.CharField(max_length=200)
    issue = models.ForeignKey("issues.Issue", null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    project = models.ForeignKey("projects.Project", null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    excerpt = models.CharField(max_length=300, blank=True)
    read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["recipient", "workspace", "-created_at"]), models.Index(fields=["recipient", "workspace", "read"])]
        constraints = [
            models.CheckConstraint(condition=Q(category__in=["assignment", "mention", "comment", "update"]), name="notification_category_valid"),
        ]
