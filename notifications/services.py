"""
Creates notifications as a side effect of issue changes and comments.

Recipients are always filtered to people who can still see the issue's
project (access.effective_project_role), so a notification never reveals a
private project, and the actor is never notified about their own action.
"""

import re

from django.contrib.auth import get_user_model

from issues.models import Issue
from notifications.models import Notification
from workspaces import access
from workspaces.models import WorkspaceMembership

User = get_user_model()
STATUS_LABELS = dict(Issue.Status.choices)
MENTION_PATTERN = re.compile(r"(?<![\w@])@([\w.\-]+)")


def _visible_recipients(issue, user_ids, actor):
    """Users from `user_ids` who are workspace members and can view the issue's project."""
    project = issue.project
    ids = {uid for uid in user_ids if uid and (actor is None or uid != actor.id)}
    if not ids:
        return []
    memberships = WorkspaceMembership.objects.filter(workspace_id=project.workspace_id, user_id__in=ids).select_related("user")
    return [m.user for m in memberships if access.effective_project_role(m, project) is not None]


def _create(recipients, *, issue, actor, category, summary, excerpt=""):
    Notification.objects.bulk_create(
        Notification(
            recipient=user,
            workspace_id=issue.project.workspace_id,
            actor=actor,
            category=category,
            summary=summary,
            issue=issue,
            project=issue.project,
            excerpt=excerpt[:300],
        )
        for user in recipients
    )


def issue_changed(issue, actor, before):
    """Assignment and status changes. `before` is activity.services.snapshot(issue)."""
    if issue.assignee_id and issue.assignee_id != before["assignee_id"]:
        _create(
            _visible_recipients(issue, [issue.assignee_id], actor),
            issue=issue, actor=actor, category=Notification.Category.ASSIGNMENT, summary="assigned you to",
        )
    if issue.status != before["status"]:
        watchers = [issue.assignee_id, issue.reporter_id]
        if issue.assignee_id != before["assignee_id"]:
            watchers.remove(issue.assignee_id)  # already got the assignment notification
        _create(
            _visible_recipients(issue, watchers, actor),
            issue=issue, actor=actor, category=Notification.Category.UPDATE,
            summary=f"moved to {STATUS_LABELS[issue.status]}",
        )


def issue_created(issue, actor):
    if issue.assignee_id:
        _create(
            _visible_recipients(issue, [issue.assignee_id], actor),
            issue=issue, actor=actor, category=Notification.Category.ASSIGNMENT, summary="assigned you to",
        )


def mentioned_user_ids(issue, body):
    """@first-name, @full.name / @full-name or @email-local-part of workspace members."""
    handles = {h.lower().rstrip(".-") for h in MENTION_PATTERN.findall(body)}
    if not handles:
        return set()
    found = set()
    for user in User.objects.filter(workspace_memberships__workspace_id=issue.project.workspace_id).only("id", "full_name", "email"):
        names = {user.email.split("@")[0].lower()}
        if user.full_name:
            parts = user.full_name.lower().split()
            names |= {parts[0], ".".join(parts), "-".join(parts), "".join(parts)}
        if names & handles:
            found.add(user.id)
    return found


def comment_added(issue, comment, actor):
    mentioned = mentioned_user_ids(issue, comment.body)
    _create(
        _visible_recipients(issue, mentioned, actor),
        issue=issue, actor=actor, category=Notification.Category.MENTION, summary="mentioned you in", excerpt=comment.body,
    )
    # Followers: assignee, reporter and earlier commenters
    followers = {issue.assignee_id, issue.reporter_id}
    followers |= set(issue.comments.exclude(author=None).values_list("author_id", flat=True))
    _create(
        _visible_recipients(issue, followers - mentioned, actor),
        issue=issue, actor=actor, category=Notification.Category.COMMENT, summary="commented on", excerpt=comment.body,
    )
