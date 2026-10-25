"""
Issue business logic shared by the create/update/move/bulk endpoints and the
sprint service. Every write runs in a transaction, records activity and
creates notifications. Authorization happens in the views before calling in.
"""

from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from activity import services as activity
from activity.models import Activity
from issues.models import Issue
from notifications import services as notify
from projects.models import Project

RANK_STEP = 1000.0
MIN_RANK_GAP = 1e-6


def _set_completion(issue, previous_status):
    if issue.status == Issue.Status.DONE and previous_status != Issue.Status.DONE:
        issue.completed_at = timezone.now()
    elif issue.status != Issue.Status.DONE:
        issue.completed_at = None


def create_issue(actor, validated_data):
    project = validated_data["project"]
    with transaction.atomic():
        # Row lock: issue numbers are allocated sequentially per project
        locked = Project.objects.select_for_update().get(pk=project.pk)
        locked.issue_counter += 1
        locked.save(update_fields=["issue_counter", "updated_at"])
        top = Issue.objects.filter(project=locked).aggregate(m=Max("rank"))["m"] or 0
        issue = Issue(
            **validated_data,
            number=locked.issue_counter,
            key=f"{locked.key}-{locked.issue_counter}",
            reporter=actor,
            rank=top + RANK_STEP,
        )
        if issue.issue_type not in Issue.PLANNABLE_TYPES:
            issue.sprint = None
        _set_completion(issue, None)
        issue.save()
        activity.record(issue, actor, Activity.Kind.CREATED)
        notify.issue_created(issue, actor)
    return issue


def update_issue(actor, issue, changes):
    """Apply validated field changes to one issue (caller holds a transaction)."""
    before = activity.snapshot(issue)
    for field, value in changes.items():
        setattr(issue, field, value)
    if issue.issue_type not in Issue.PLANNABLE_TYPES:
        issue.sprint = None
    _set_completion(issue, before["status"])
    issue.save()
    activity.record_changes(issue, actor, before)
    notify.issue_changed(issue, actor, before)
    return issue


def _renumber(project_id):
    """Spread ranks out again when two neighbours got too close."""
    issues = list(Issue.objects.select_for_update().filter(project_id=project_id).order_by("rank", "id"))
    for index, item in enumerate(issues, start=1):
        item.rank = index * RANK_STEP
    Issue.objects.bulk_update(issues, ["rank"])


def rank_between(issue, before_key, after_key):
    """Rank placing `issue` between the issues `before_key` (above) and `after_key` (below)."""
    neighbours = {
        i.key: i for i in Issue.objects.filter(project_id=issue.project_id, key__in=[k for k in (before_key, after_key) if k])
    }
    before = neighbours.get(before_key) if before_key else None
    after = neighbours.get(after_key) if after_key else None
    if (before_key and before is None) or (after_key and after is None):
        raise ValidationError({"position": ["Neighbouring issue not found in this project."]})
    if before and after and after.rank - before.rank < MIN_RANK_GAP:
        _renumber(issue.project_id)
        before.refresh_from_db(fields=["rank"])
        after.refresh_from_db(fields=["rank"])
    if before is None and after is None:
        return issue.rank
    if before is None:
        return after.rank - RANK_STEP
    if after is None:
        return before.rank + RANK_STEP
    return (before.rank + after.rank) / 2


def move_issue(actor, issue, changes, before_key=None, after_key=None):
    with transaction.atomic():
        issue = Issue.objects.select_for_update().select_related("project").get(pk=issue.pk)
        if before_key or after_key:
            changes = {**changes, "rank": rank_between(issue, before_key, after_key)}
        return update_issue(actor, issue, changes)


def bulk_update(actor, issues, changes):
    """All-or-nothing: the caller validated and authorized every issue."""
    with transaction.atomic():
        locked = list(Issue.objects.select_for_update().select_related("project").filter(pk__in=[i.pk for i in issues]))
        return [update_issue(actor, issue, dict(changes)) for issue in locked]


def delete_issue(issue):
    """Subtasks can't exist without their parent, so they go too; epic children are kept (parent cleared)."""
    with transaction.atomic():
        Issue.objects.filter(parent=issue, issue_type=Issue.Type.SUBTASK).delete()
        issue.delete()
