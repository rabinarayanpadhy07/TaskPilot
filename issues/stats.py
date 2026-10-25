"""Aggregates computed from real issues (never from client-supplied numbers)."""

from collections import defaultdict

from django.db.models import Count, Q, Sum
from django.utils import timezone

from issues.models import Issue


def empty_issue_counts():
    return {"total": 0, "done": 0, "open": 0, "overdue": 0, "by_status": {}, "by_type": {}}


def issue_counts_by_project(project_ids):
    """{project_id: issue_counts} in two grouped queries. Epics count only in by_type."""
    counts = defaultdict(empty_issue_counts)
    today = timezone.localdate()
    rows = (
        Issue.objects.filter(project_id__in=project_ids)
        .values("project_id", "issue_type", "status")
        .annotate(n=Count("id"), overdue=Count("id", filter=Q(due_date__lt=today) & ~Q(status=Issue.Status.DONE)))
    )
    for row in rows:
        c = counts[row["project_id"]]
        c["by_type"][row["issue_type"]] = c["by_type"].get(row["issue_type"], 0) + row["n"]
        if row["issue_type"] == Issue.Type.EPIC:
            continue
        c["total"] += row["n"]
        c["by_status"][row["status"]] = c["by_status"].get(row["status"], 0) + row["n"]
        if row["status"] == Issue.Status.DONE:
            c["done"] += row["n"]
        else:
            c["open"] += row["n"]
        c["overdue"] += row["overdue"]
    return counts


def sprint_stats(sprint):
    plannable = sprint.issues.filter(issue_type__in=Issue.PLANNABLE_TYPES)
    totals = plannable.aggregate(
        total=Count("id"),
        done=Count("id", filter=Q(status=Issue.Status.DONE)),
        points=Sum("story_points"),
        done_points=Sum("story_points", filter=Q(status=Issue.Status.DONE)),
    )
    by_status = {row["status"]: row["n"] for row in plannable.values("status").annotate(n=Count("id"))}
    return {
        "total": totals["total"],
        "done": totals["done"],
        "points": totals["points"] or 0,
        "done_points": totals["done_points"] or 0,
        "by_status": by_status,
    }
