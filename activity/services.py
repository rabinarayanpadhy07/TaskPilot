"""Records issue history. Called by the issue/sprint/comment/attachment services only."""

from datetime import date

from activity.models import Activity

# Model attribute -> API field name used in activity entries (the frontend maps these back)
TRACKED_FIELDS = {
    "title": "title",
    "description": "description",
    "status": "status",
    "priority": "priority",
    "issue_type": "issue_type",
    "assignee_id": "assignee",
    "reporter_id": "reporter",
    "labels": "labels",
    "due_date": "due_date",
    "story_points": "story_points",
    "parent_id": "parent",
    "sprint_id": "sprint",
}


def _json(value):
    if isinstance(value, date):
        return value.isoformat()
    return value


def snapshot(issue):
    """Current values of the tracked fields, for diffing."""
    return {attr: getattr(issue, attr) for attr in TRACKED_FIELDS}


def record(issue, actor, kind, field="", from_value=None, to_value=None):
    return Activity.objects.create(
        workspace_id=issue.project.workspace_id,
        issue=issue,
        actor=actor,
        kind=kind,
        field=field,
        from_value=_json(from_value),
        to_value=_json(to_value),
    )


def record_changes(issue, actor, before):
    """One `updated` entry per changed field. Description values are omitted (can be long)."""
    entries = []
    for attr, api_field in TRACKED_FIELDS.items():
        old, new = before[attr], getattr(issue, attr)
        if old == new:
            continue
        hide = api_field == "description"
        entries.append(
            Activity(
                workspace_id=issue.project.workspace_id,
                issue=issue,
                actor=actor,
                kind=Activity.Kind.UPDATED,
                field=api_field,
                from_value=None if hide else _json(old),
                to_value=None if hide else _json(new),
            )
        )
    Activity.objects.bulk_create(entries)
    return entries
