from django.db import models
from django.db.models import F, Q

from projects.models import Project


class Sprint(models.Model):
    class Status(models.TextChoices):
        PLANNED = "planned", "Planned"
        ACTIVE = "active", "Active"
        COMPLETED = "completed", "Completed"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="sprints")
    name = models.CharField(max_length=80)
    goal = models.TextField(blank=True, max_length=2000)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PLANNED)
    completed_at = models.DateTimeField(null=True, blank=True)
    # Velocity snapshots: points planned when the sprint started, and points
    # done when it completed (open issues leave the sprint on completion).
    committed_points = models.PositiveIntegerField(null=True, blank=True)
    completed_points = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["start_date", "id"]
        indexes = [models.Index(fields=["project", "status"])]
        constraints = [
            models.CheckConstraint(condition=Q(status__in=["planned", "active", "completed"]), name="sprint_status_valid"),
            models.CheckConstraint(
                condition=Q(start_date__isnull=True) | Q(end_date__isnull=True) | Q(end_date__gt=F("start_date")),
                name="sprint_end_after_start",
            ),
            models.CheckConstraint(
                condition=~Q(status="active") | (Q(start_date__isnull=False) & Q(end_date__isnull=False)),
                name="active_sprint_has_dates",
            ),
            # Business rule: at most one active sprint per project
            models.UniqueConstraint(fields=["project"], condition=Q(status="active"), name="one_active_sprint_per_project"),
        ]

    def __str__(self):
        return self.name
