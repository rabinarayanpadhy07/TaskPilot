"""
TaskPilot role & permission matrix: the single source of truth.

Two layers of access:

1. **Workspace role** (WorkspaceMembership.role): what someone can do in the
   workspace as a whole: manage people, create projects, see every project.
2. **Project role** (effective per project, see access.effective_project_role):
   what someone can do inside one project: view, contribute, manage.

Views never compare role names. They ask for a permission (`WorkspacePerm` /
`ProjectPerm`) and access.py resolves it from the caller's membership rows in
the database. A role or workspace sent by the client is never trusted.

The frontend mirrors this table in src/models/roles.js for display only, and
docs/authorization.md documents it. Keep all three in sync.
"""

from django.db import models


class WorkspaceRole(models.TextChoices):
    OWNER = "owner", "Owner"
    ADMIN = "admin", "Admin"
    PROJECT_MANAGER = "project_manager", "Project Manager"
    DEVELOPER = "developer", "Developer"
    VIEWER = "viewer", "Viewer"


# Higher rank = more authority. Used for "you can only manage people below you".
WORKSPACE_ROLE_RANK = {
    WorkspaceRole.OWNER: 50,
    WorkspaceRole.ADMIN: 40,
    WorkspaceRole.PROJECT_MANAGER: 30,
    WorkspaceRole.DEVELOPER: 20,
    WorkspaceRole.VIEWER: 10,
}


class WorkspacePerm(models.TextChoices):
    VIEW = "workspace.view", "View the workspace"
    UPDATE = "workspace.update", "Edit workspace settings"
    DELETE = "workspace.delete", "Delete the workspace"
    MEMBERS_VIEW = "members.view", "See workspace members"
    MEMBERS_INVITE = "members.invite", "Invite people and manage invitations"
    MEMBERS_MANAGE = "members.manage", "Change roles and remove members"
    PROJECTS_CREATE = "projects.create", "Create projects"
    PROJECTS_VIEW_ALL = "projects.view_all", "See every project, including private ones"
    PROJECTS_MANAGE_ALL = "projects.manage_all", "Manage every project"
    PROJECTS_DELETE = "projects.delete", "Delete projects"
    REPORTS_VIEW = "reports.view", "View reports for accessible projects"


_BASE = {WorkspacePerm.VIEW, WorkspacePerm.MEMBERS_VIEW, WorkspacePerm.REPORTS_VIEW}

WORKSPACE_ROLE_PERMISSIONS = {
    WorkspaceRole.OWNER: frozenset(WorkspacePerm),
    WorkspaceRole.ADMIN: frozenset(set(WorkspacePerm) - {WorkspacePerm.DELETE}),
    WorkspaceRole.PROJECT_MANAGER: frozenset(_BASE | {WorkspacePerm.PROJECTS_CREATE}),
    WorkspaceRole.DEVELOPER: frozenset(_BASE),
    WorkspaceRole.VIEWER: frozenset(_BASE),
}


class ProjectRole(models.TextChoices):
    MANAGER = "manager", "Manager"
    CONTRIBUTOR = "contributor", "Contributor"
    VIEWER = "viewer", "Viewer"


PROJECT_ROLE_RANK = {ProjectRole.MANAGER: 3, ProjectRole.CONTRIBUTOR: 2, ProjectRole.VIEWER: 1}


class ProjectPerm(models.TextChoices):
    VIEW = "project.view", "View the project, its issues and sprints"
    ISSUES_WRITE = "issues.write", "Create, edit and move issues"
    ISSUES_DELETE = "issues.delete", "Delete issues"
    COMMENT = "issues.comment", "Comment on issues"
    SPRINTS_MANAGE = "sprints.manage", "Create, start and complete sprints"
    SETTINGS_UPDATE = "project.update", "Edit project details and visibility"
    MEMBERS_MANAGE = "project.members.manage", "Add, remove and change project members"


PROJECT_ROLE_PERMISSIONS = {
    ProjectRole.MANAGER: frozenset(ProjectPerm),
    ProjectRole.CONTRIBUTOR: frozenset({ProjectPerm.VIEW, ProjectPerm.ISSUES_WRITE, ProjectPerm.COMMENT}),
    ProjectRole.VIEWER: frozenset({ProjectPerm.VIEW}),
}

# The highest project role a workspace role may hold. Adding a Viewer to a
# project as "manager" still only gives them viewer access.
MAX_PROJECT_ROLE = {
    WorkspaceRole.OWNER: ProjectRole.MANAGER,
    WorkspaceRole.ADMIN: ProjectRole.MANAGER,
    WorkspaceRole.PROJECT_MANAGER: ProjectRole.MANAGER,
    WorkspaceRole.DEVELOPER: ProjectRole.CONTRIBUTOR,
    WorkspaceRole.VIEWER: ProjectRole.VIEWER,
}

# Access to a workspace-visible project for someone who isn't a project member.
# Private projects grant nothing without membership (except PROJECTS_VIEW_ALL).
DEFAULT_PROJECT_ROLE = {
    WorkspaceRole.OWNER: ProjectRole.MANAGER,
    WorkspaceRole.ADMIN: ProjectRole.MANAGER,
    WorkspaceRole.PROJECT_MANAGER: ProjectRole.CONTRIBUTOR,
    WorkspaceRole.DEVELOPER: ProjectRole.CONTRIBUTOR,
    WorkspaceRole.VIEWER: ProjectRole.VIEWER,
}


def assignable_roles(actor_role):
    """
    Roles `actor_role` may grant (invite as, or change someone to).
    Owners may grant any role; everyone else only roles below their own.
    """
    if actor_role == WorkspaceRole.OWNER:
        return list(WorkspaceRole)
    rank = WORKSPACE_ROLE_RANK[actor_role]
    return [role for role in WorkspaceRole if WORKSPACE_ROLE_RANK[role] < rank]


def can_manage_role(actor_role, target_role):
    """May `actor_role` change or remove someone who currently holds `target_role`?"""
    if actor_role == WorkspaceRole.OWNER:
        return True
    return WORKSPACE_ROLE_RANK[target_role] < WORKSPACE_ROLE_RANK[actor_role]


def cap_project_role(project_role, workspace_role):
    """Clamp a project role to what the workspace role allows."""
    ceiling = MAX_PROJECT_ROLE[workspace_role]
    return project_role if PROJECT_ROLE_RANK[project_role] <= PROJECT_ROLE_RANK[ceiling] else ceiling
