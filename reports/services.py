"""
Workspace reports, aggregated on the server from issues the caller can see.

Scope: always access.visible_projects(membership), optionally narrowed to
one project. A project id outside that set is a 404, so reports can't be used
to probe private projects or other workspaces.

Snapshots (status distribution, workload, overdue, epic progress) describe
the current state. The date range applies to completions over time and to
sprint velocity.
"""

from datetime import date, timedelta

from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from issues.models import Issue
from issues.stats import sprint_stats
from sprints.models import Sprint

MAX_RANGE_DAYS = 366
DAILY_BUCKET_MAX_DAYS = 31
OVERDUE_LIMIT = 50
OPEN_STATUSES = [Issue.Status.BACKLOG, Issue.Status.TODO, Issue.Status.IN_PROGRESS, Issue.Status.IN_REVIEW]


def _buckets(date_from, date_to):
    """Daily buckets for short ranges, Monday-start weeks otherwise."""
    days = (date_to - date_from).days + 1
    if days <= DAILY_BUCKET_MAX_DAYS:
        return "day", [(date_from + timedelta(d), date_from + timedelta(d)) for d in range(days)]
    start = date_from - timedelta(days=date_from.weekday())
    buckets = []
    while start <= date_to:
        buckets.append((max(start, date_from), min(start + timedelta(days=6), date_to)))
        start += timedelta(days=7)
    return "week", buckets


def completed_over_time(issues, date_from, date_to):
    granularity, buckets = _buckets(date_from, date_to)
    per_day = (
        issues.filter(status=Issue.Status.DONE, completed_at__date__gte=date_from, completed_at__date__lte=date_to)
        .annotate(day=TruncDate("completed_at"))
        .values("day")
        .annotate(n=Count("id"), points=Sum("story_points"))
    )
    by_day = {row["day"]: row for row in per_day}
    series = []
    for start, end in buckets:
        count = points = 0
        day = start
        while day <= end:
            if day in by_day:
                count += by_day[day]["n"]
                points += by_day[day]["points"] or 0
            day += timedelta(days=1)
        series.append({"start": start, "end": end, "count": count, "points": points})
    return granularity, series


def sprint_velocity(projects, date_from, date_to):
    sprints = (
        Sprint.objects.filter(project__in=projects)
        .filter(
            Q(status=Sprint.Status.ACTIVE)
            | Q(status=Sprint.Status.COMPLETED, completed_at__date__gte=date_from, completed_at__date__lte=date_to)
        )
        .select_related("project")
        .order_by("end_date", "id")
    )
    rows = []
    for sprint in sprints:
        stats = sprint_stats(sprint)
        completed = sprint.status == Sprint.Status.COMPLETED
        rows.append({
            "id": sprint.id,
            "name": sprint.name,
            "project": sprint.project_id,
            "project_key": sprint.project.key,
            "status": sprint.status,
            "start_date": sprint.start_date,
            "end_date": sprint.end_date,
            "committed_points": sprint.committed_points if sprint.committed_points is not None else stats["points"],
            "completed_points": sprint.completed_points if completed and sprint.completed_points is not None else stats["done_points"],
            "total": stats["total"],
            "done": stats["done"],
        })
    return rows


def workload(issues):
    rows = (
        issues.filter(status__in=OPEN_STATUSES)
        .values("assignee_id", "assignee__full_name", "assignee__email", "status")
        .annotate(n=Count("id"), points=Sum("story_points"))
    )
    today = timezone.localdate()
    overdue = dict(
        issues.filter(status__in=OPEN_STATUSES, due_date__lt=today)
        .values("assignee_id")
        .annotate(n=Count("id"))
        .values_list("assignee_id", "n")
    )
    people = {}
    for row in rows:
        entry = people.setdefault(row["assignee_id"], {
            "assignee": None if row["assignee_id"] is None else {
                "id": row["assignee_id"], "full_name": row["assignee__full_name"], "email": row["assignee__email"],
            },
            "by_status": {},
            "open": 0,
            "points": 0,
            "overdue": overdue.get(row["assignee_id"], 0),
        })
        entry["by_status"][row["status"]] = row["n"]
        entry["open"] += row["n"]
        entry["points"] += row["points"] or 0
    return sorted(people.values(), key=lambda p: (p["assignee"] is None, -p["open"]))


def overdue_issues(issues):
    today = timezone.localdate()
    qs = issues.filter(status__in=OPEN_STATUSES, due_date__lt=today).select_related("project").order_by("due_date", "id")
    return qs.count(), [
        {
            "key": i.key, "title": i.title, "project": i.project_id, "issue_type": i.issue_type,
            "priority": i.priority, "status": i.status, "assignee": i.assignee_id, "due_date": i.due_date,
            "days_overdue": (today - i.due_date).days,
        }
        for i in qs[:OVERDUE_LIMIT]
    ]


def epic_progress(issues):
    epics = issues.filter(issue_type=Issue.Type.EPIC).select_related("project")
    children = (
        Issue.objects.filter(parent__in=epics)
        .values("parent_id")
        .annotate(
            total=Count("id"),
            done=Count("id", filter=Q(status=Issue.Status.DONE)),
            points=Sum("story_points"),
            done_points=Sum("story_points", filter=Q(status=Issue.Status.DONE)),
        )
    )
    by_epic = {row["parent_id"]: row for row in children}
    rows = []
    for epic in epics:
        c = by_epic.get(epic.id, {})
        rows.append({
            "key": epic.key, "title": epic.title, "project": epic.project_id, "status": epic.status,
            "due_date": epic.due_date, "total": c.get("total", 0), "done": c.get("done", 0),
            "points": c.get("points") or 0, "done_points": c.get("done_points") or 0,
        })
    return sorted(rows, key=lambda r: (r["status"] == Issue.Status.DONE, r["total"] == 0, r["key"]))


def build_report(projects, date_from: date, date_to: date):
    issues = Issue.objects.filter(project__in=projects)
    work = issues.exclude(issue_type=Issue.Type.EPIC)
    by_status = dict(work.values("status").annotate(n=Count("id")).values_list("status", "n"))
    granularity, completed = completed_over_time(work, date_from, date_to)
    overdue_count, overdue = overdue_issues(work)
    return {
        "range": {"from": date_from, "to": date_to, "granularity": granularity},
        "projects": list(projects.values_list("id", flat=True)),
        "totals": {
            "issues": sum(by_status.values()),
            "open": sum(n for s, n in by_status.items() if s != Issue.Status.DONE),
            "completed_in_range": sum(b["count"] for b in completed),
            "overdue": overdue_count,
        },
        "status_distribution": [{"status": s, "count": by_status.get(s, 0)} for s in Issue.Status.values],
        "completed_over_time": completed,
        "sprint_velocity": sprint_velocity(projects, date_from, date_to),
        "workload": workload(work),
        "overdue": overdue,
        "epic_progress": epic_progress(issues),
    }
