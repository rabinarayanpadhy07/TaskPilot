"""
Authorization policy: resolves what a user may do from database rows only.

Inputs are always the authenticated `request.user` and objects loaded from the
database. Nothing here accepts a role, a workspace id or a permission from the
client. Views and serializers must go through these helpers (or the DRF classes
in workspaces/permissions.py) instead of comparing role names themselves.
"""

from django.db.models import Exists, OuterRef, Q

from workspaces.models import WorkspaceMembership
from workspaces.roles import (
    DEFAULT_PROJECT_ROLE,
    PROJECT_ROLE_PERMISSIONS,
    PROJECT_ROLE_RANK,
    WORKSPACE_ROLE_PERMISSIONS,
    ProjectPerm,
    WorkspacePerm,
    WorkspaceRole,
    assignable_roles,
    can_manage_role,
    cap_project_role,
)

_UNSET = object()


def get_membership(user, workspace_id):
    """The caller's membership in `workspace_id`, or None. Never raises for bad ids."""
    if not user or not user.is_authenticated:
        return None
    try:
        workspace_id = int(workspace_id)
    except (TypeError, ValueError):
        return None
    return (
        WorkspaceMembership.objects.select_related("workspace", "user")
        .filter(workspace_id=workspace_id, user=user, user__is_active=True)
        .first()
    )


# ---------- Workspace level ----------


def workspace_permissions(membership):
    if membership is None:
        return frozenset()
    return WORKSPACE_ROLE_PERMISSIONS[WorkspaceRole(membership.role)]


def has_workspace_perm(membership, perm):
    return perm in workspace_permissions(membership)


def can_manage_member(actor, target):
    """May `actor` change the role of / remove `target` (both memberships of one workspace)?"""
    if actor is None or target is None or actor.workspace_id != target.workspace_id:
        return False
    if actor.pk == target.pk:
        return False  # self-service is "leave", handled separately
    return has_workspace_perm(actor, WorkspacePerm.MEMBERS_MANAGE) and can_manage_role(actor.role, target.role)


def roles_assignable_by(membership):
    """Roles this member may grant through invitations or role changes."""
    if not (has_workspace_perm(membership, WorkspacePerm.MEMBERS_INVITE) or has_workspace_perm(membership, WorkspacePerm.MEMBERS_MANAGE)):
        return []
    return assignable_roles(WorkspaceRole(membership.role))


# ---------- Project level ----------


def effective_project_role(membership, project, project_membership=_UNSET):
    """
    The caller's role in `project`, or None when they can't see it.

    - Project in another workspace -> None (never cross workspaces).
    - Owners/admins -> manager everywhere.
    - Explicit ProjectMembership -> its role, capped by the workspace role.
    - Workspace-visible project -> at least the workspace role's default.
    - Private project without membership -> None.
    """
    if membership is None or project.workspace_id != membership.workspace_id:
        return None
    ws_role = WorkspaceRole(membership.role)
    if has_workspace_perm(membership, WorkspacePerm.PROJECTS_MANAGE_ALL):
        return DEFAULT_PROJECT_ROLE[ws_role]

    if project_membership is _UNSET:
        project_membership = project.memberships.filter(user_id=membership.user_id).first()

    candidates = []
    if project_membership is not None:
        candidates.append(cap_project_role(project_membership.role, ws_role))
    if project.visibility == project.Visibility.WORKSPACE:
        candidates.append(DEFAULT_PROJECT_ROLE[ws_role])
    if not candidates:
        return None
    return max(candidates, key=lambda role: PROJECT_ROLE_RANK[role])


def project_permissions(membership, project, project_membership=_UNSET):
    role = effective_project_role(membership, project, project_membership)
    return PROJECT_ROLE_PERMISSIONS[role] if role else frozenset()


def has_project_perm(membership, project, perm):
    return perm in project_permissions(membership, project)


def project_permissions_map(membership, projects):
    """{project_id: permissions} for many projects with one membership query (list endpoints)."""
    from projects.models import ProjectMembership

    if membership is None:
        return {}
    explicit = {
        pm.project_id: pm
        for pm in ProjectMembership.objects.filter(user_id=membership.user_id, project__in=[p.pk for p in projects])
    }
    return {p.pk: project_permissions(membership, p, explicit.get(p.pk)) for p in projects}


def visible_projects(membership):
    """
    Projects the caller may see, always restricted to their workspace. Use this
    as the base queryset for anything project-scoped (issues, sprints, reports).
    """
    from projects.models import Project, ProjectMembership

    if membership is None:
        return Project.objects.none()
    qs = Project.objects.filter(workspace_id=membership.workspace_id)
    if has_workspace_perm(membership, WorkspacePerm.PROJECTS_VIEW_ALL):
        return qs
    is_member = Exists(ProjectMembership.objects.filter(project=OuterRef("pk"), user_id=membership.user_id))
    return qs.filter(Q(visibility=Project.Visibility.WORKSPACE) | Q(is_member))


def assert_project_perm(membership, project, perm, message=None):
    """Raise 404 if the project isn't visible, 403 if visible but `perm` is missing."""
    from rest_framework.exceptions import NotFound, PermissionDenied

    perms = project_permissions(membership, project)
    if ProjectPerm.VIEW not in perms:
        raise NotFound()
    if perm not in perms:
        raise PermissionDenied(message or "You do not have permission to perform this action.")
