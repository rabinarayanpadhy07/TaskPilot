from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.core.validators import MaxValueValidator
from django.db import models
from django.db.models import Q

from projects.models import Project


class Issue(models.Model):
    """Epics, stories, tasks, bugs and subtasks. Epics are issues with issue_type="epic"."""

    class Type(models.TextChoices):
        EPIC = "epic", "Epic"
        STORY = "story", "Story"
        TASK = "task", "Task"
        BUG = "bug", "Bug"
        SUBTASK = "subtask", "Subtask"

    class Status(models.TextChoices):
        BACKLOG = "backlog", "Backlog"
        TODO = "todo", "To do"
        IN_PROGRESS = "in_progress", "In progress"
        IN_REVIEW = "in_review", "In review"
        DONE = "done", "Done"

    class Priority(models.TextChoices):
        HIGHEST = "highest", "Highest"
        HIGH = "high", "High"
        MEDIUM = "medium", "Medium"
        LOW = "low", "Low"
        LOWEST = "lowest", "Lowest"

    PLANNABLE_TYPES = {Type.STORY, Type.TASK, Type.BUG}

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="issues")
    number = models.PositiveIntegerField(editable=False)
    key = models.CharField(max_length=24, editable=False)
    issue_type = models.CharField(max_length=10, choices=Type.choices, default=Type.TASK)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, max_length=20000)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.TODO)
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.MEDIUM)
    assignee = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="assigned_issues")
    reporter = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="reported_issues")
    labels = models.JSONField(default=list, blank=True)
    due_date = models.DateField(null=True, blank=True)
    story_points = models.PositiveSmallIntegerField(null=True, blank=True, validators=[MaxValueValidator(100)])
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="children")
    sprint = models.ForeignKey("sprints.Sprint", null=True, blank=True, on_delete=models.SET_NULL, related_name="issues")
    rank = models.FloatField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["rank", "id"]
        constraints = [
            models.UniqueConstraint(fields=["project", "number"], name="unique_issue_number_per_project"),
            models.UniqueConstraint(fields=["project", "key"], name="unique_issue_key_per_project"),
            models.CheckConstraint(condition=Q(issue_type__in=["epic", "story", "task", "bug", "subtask"]), name="issue_type_valid"),
            models.CheckConstraint(condition=Q(status__in=["backlog", "todo", "in_progress", "in_review", "done"]), name="issue_status_valid"),
            models.CheckConstraint(condition=Q(priority__in=["highest", "high", "medium", "low", "lowest"]), name="issue_priority_valid"),
            models.CheckConstraint(condition=Q(story_points__isnull=True) | Q(story_points__lte=100), name="issue_points_range"),
            # completed_at is set exactly when the issue is done
            models.CheckConstraint(
                condition=(Q(status="done") & Q(completed_at__isnull=False)) | (~Q(status="done") & Q(completed_at__isnull=True)),
                name="issue_completed_at_matches_status",
            ),
            # Only stories, tasks and bugs are planned in sprints
            models.CheckConstraint(
                condition=Q(sprint__isnull=True) | Q(issue_type__in=["story", "task", "bug"]),
                name="issue_sprint_only_plannable",
            ),
        ]
        indexes = [
            models.Index(fields=["project", "status"]),
            models.Index(fields=["project", "rank"]),
            models.Index(fields=["assignee", "status"]),
            models.Index(fields=["sprint", "status"]),
            models.Index(fields=["parent"]),
            models.Index(fields=["project", "-updated_at"]),
            models.Index(fields=["completed_at"]),
            GinIndex(fields=["labels"], name="issue_labels_gin"),
        ]

    def __str__(self):
        return f"{self.key} {self.title}"

    @property
    def is_plannable(self):
        return self.issue_type in self.PLANNABLE_TYPES


class Comment(models.Model):
    issue = models.ForeignKey(Issue, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="comments")
    body = models.TextField(max_length=5000)
    created_at = models.DateTimeField(auto_now_add=True)
    edited_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["issue", "created_at"])]
        constraints = [models.CheckConstraint(condition=~Q(body=""), name="comment_body_not_empty")]

    def __str__(self):
        return f"Comment on {self.issue.key}"


def attachment_path(instance, filename):
    # Random directory per upload: the original name is kept in file_name
    import uuid

    return f"attachments/{instance.issue.project_id}/{uuid.uuid4().hex}/{filename[-100:]}"


class Attachment(models.Model):
    issue = models.ForeignKey(Issue, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to=attachment_path, max_length=255)
    file_name = models.CharField(max_length=255)
    size = models.PositiveIntegerField()
    content_type = models.CharField(max_length=120)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["issue", "-created_at"])]

    def __str__(self):
        return self.file_name
