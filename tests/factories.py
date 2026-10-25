import itertools
from datetime import timedelta

from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from issues.models import Issue
from projects.models import Project, ProjectMembership
from sprints.models import Sprint
from workspaces.models import Workspace, WorkspaceMembership
from workspaces.roles import ProjectRole, WorkspaceRole

_seq = itertools.count(1)


def make_user(name=None, email=None):
    n = next(_seq)
    return User.objects.create_user(email=email or f"user{n}@example.com", password="s3cure-pass-123", full_name=name or f"User {n}")


def make_workspace(owner=None, name=None):
    n = next(_seq)
    workspace = Workspace.objects.create(name=name or f"Workspace {n}", slug=f"ws-{n}")
    if owner:
        add_member(workspace, owner, WorkspaceRole.OWNER)
    return workspace


def add_member(workspace, user, role):
    return WorkspaceMembership.objects.create(workspace=workspace, user=user, role=role)


def make_project(workspace, key=None, visibility=Project.Visibility.WORKSPACE, members=()):
    project = Project.objects.create(workspace=workspace, key=key or f"P{next(_seq)}", name="Project", visibility=visibility)
    for user, role in members:
        ProjectMembership.objects.create(project=project, user=user, role=role)
    return project


def make_issue(project, issue_type=Issue.Type.TASK, status=Issue.Status.TODO, **extra):
    project.issue_counter += 1
    project.save(update_fields=["issue_counter"])
    return Issue.objects.create(
        project=project,
        number=project.issue_counter,
        key=f"{project.key}-{project.issue_counter}",
        issue_type=issue_type,
        title=extra.pop("title", "Issue"),
        status=status,
        completed_at=extra.pop("completed_at", timezone.now() if status == Issue.Status.DONE else None),
        **extra,
    )


def make_sprint(project, status=Sprint.Status.PLANNED, **extra):
    today = timezone.localdate()
    return Sprint.objects.create(
        project=project,
        name=extra.pop("name", "Sprint"),
        status=status,
        start_date=extra.pop("start_date", today - timedelta(days=3)),
        end_date=extra.pop("end_date", today + timedelta(days=10)),
        **extra,
    )


def client_for(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


class Team:
    """One workspace with one member per role, plus an unrelated workspace."""

    def __init__(self):
        self.owner = make_user("Olive Owner")
        self.admin = make_user("Ada Admin")
        self.pm = make_user("Pat Manager")
        self.dev = make_user("Dev Eloper")
        self.viewer = make_user("Vic Viewer")
        self.outsider = make_user("Out Sider")

        self.ws = make_workspace(self.owner, "Acme")
        add_member(self.ws, self.admin, WorkspaceRole.ADMIN)
        add_member(self.ws, self.pm, WorkspaceRole.PROJECT_MANAGER)
        add_member(self.ws, self.dev, WorkspaceRole.DEVELOPER)
        add_member(self.ws, self.viewer, WorkspaceRole.VIEWER)

        self.other_ws = make_workspace(self.outsider, "Other Co")
        self.other_project = make_project(self.other_ws, key="OTH")
        self.other_issue = make_issue(self.other_project, title="Secret elsewhere")

        self.public = make_project(self.ws, key="PUB", members=[(self.pm, ProjectRole.MANAGER)])
        self.private = make_project(self.ws, key="PRIV", visibility=Project.Visibility.PRIVATE, members=[(self.pm, ProjectRole.MANAGER)])

    def url(self, path="", ws=None):
        return f"/api/workspaces/{(ws or self.ws).pk}/{path}"


def results(response):
    """Rows of a (paginated) list response."""
    data = response.json()
    return data["results"] if isinstance(data, dict) and "results" in data else data
