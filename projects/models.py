from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q

from workspaces.models import Workspace
from workspaces.roles import ProjectRole

project_key_validator = RegexValidator(r"^[A-Z][A-Z0-9]{1,9}$", "Use 2–10 uppercase letters or digits, starting with a letter.")


class Project(models.Model):
    class Status(models.TextChoices):
        ON_TRACK = "on_track", "On track"
        AT_RISK = "at_risk", "At risk"
        OFF_TRACK = "off_track", "Off track"
        PLANNING = "planning", "Planning"
        COMPLETED = "completed", "Completed"

    class Visibility(models.TextChoices):
        WORKSPACE = "workspace", "Workspace"
        PRIVATE = "private", "Private"

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="projects")
    key = models.CharField(max_length=10, validators=[project_key_validator])
    name = models.CharField(max_length=80)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PLANNING)
    visibility = models.CharField(max_length=20, choices=Visibility.choices, default=Visibility.WORKSPACE)
    lead = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    issue_counter = models.PositiveIntegerField(default=0, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["workspace", "key"], name="unique_project_key_per_workspace"),
            models.CheckConstraint(condition=Q(status__in=["on_track", "at_risk", "off_track", "planning", "completed"]), name="project_status_valid"),
            models.CheckConstraint(condition=Q(visibility__in=["workspace", "private"]), name="project_visibility_valid"),
        ]
        indexes = [models.Index(fields=["workspace", "visibility"])]

    def __str__(self):
        return f"{self.key} · {self.name}"


class ProjectMembership(models.Model):
    """
    Explicit project access. Required for private projects; on workspace-visible
    projects it can raise someone's role (e.g. make a Project Manager the
    project's manager). The effective role is always capped by the workspace
    role (roles.MAX_PROJECT_ROLE).
    """

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="project_memberships")
    role = models.CharField(max_length=20, choices=ProjectRole.choices, default=ProjectRole.CONTRIBUTOR)
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["project", "user"], name="unique_project_member"),
            models.CheckConstraint(condition=Q(role__in=["manager", "contributor", "viewer"]), name="project_member_role_valid"),
        ]
        indexes = [models.Index(fields=["user", "project"])]

    def __str__(self):
        return f"{self.user} in {self.project} ({self.role})"
