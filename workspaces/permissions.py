"""
DRF integration of the authorization policy (workspaces/access.py).

Every workspace-scoped view:
- inherits `WorkspaceScopedMixin`, which resolves `self.membership` from the
  workspace id in the URL and `request.user`. Nothing from the request body is
  used.
- lists `IsWorkspaceMember` in `permission_classes`: non-members get 404 so
  workspace ids can't be probed, and each action must map to a permission.
- builds querysets from `self.workspace` / `access.visible_projects(...)`, so
  objects from other workspaces or hidden projects are never found (404).

Unsafe actions that aren't listed in `workspace_perms` are DENIED. A view that
authorizes an action at project level must say so explicitly with
`PROJECT_LEVEL` and then call access.assert_project_perm(...) itself or use
`ProjectObjectPermission`.
"""

from functools import cached_property

from rest_framework.exceptions import NotFound
from rest_framework.permissions import SAFE_METHODS, BasePermission

from workspaces import access
from workspaces.roles import ProjectPerm, WorkspacePerm

# Marker: authorized per project by the view (object permission or explicit check)
PROJECT_LEVEL = "project-level"


def _action_name(view, request):
    return getattr(view, "action", None) or request.method.lower()


class WorkspaceScopedMixin:
    """For views mounted under /workspaces/<workspace_pk>/..."""

    workspace_url_kwarg = "workspace_pk"
    # {action or http method: WorkspacePerm | PROJECT_LEVEL}
    workspace_perms = {}
    # {action or http method: ProjectPerm}, used by ProjectObjectPermission
    project_perms = {}

    @cached_property
    def membership(self):
        if getattr(self, "swagger_fake_view", False):
            return None  # OpenAPI schema generation: no request user
        return access.get_membership(self.request.user, self.kwargs.get(self.workspace_url_kwarg))

    @property
    def workspace(self):
        if self.membership is None:
            raise NotFound()
        return self.membership.workspace

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["membership"] = self.membership
        return context


class IsWorkspaceMember(BasePermission):
    """Caller must be a member of the URL's workspace and hold the action's workspace permission."""

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        membership = view.membership
        if membership is None:
            raise NotFound()  # don't reveal whether the workspace exists

        action = _action_name(view, request)
        required = view.workspace_perms.get(action)
        if required is None:
            if request.method in SAFE_METHODS:
                required = WorkspacePerm.VIEW
            else:
                return False  # deny by default
        if required == PROJECT_LEVEL:
            return access.has_workspace_perm(membership, WorkspacePerm.VIEW)
        return access.has_workspace_perm(membership, required)


class ProjectObjectPermission(BasePermission):
    """
    Object-level check for project-scoped objects (projects, issues, sprints).
    The object's project must be visible to the caller (else 404) and the
    caller's effective project role must grant the action's ProjectPerm.
    """

    def has_permission(self, request, view):
        return True

    def has_object_permission(self, request, view, obj):
        project = view.get_object_project(obj) if hasattr(view, "get_object_project") else getattr(obj, "project", obj)
        perms = access.project_permissions(view.membership, project)
        if ProjectPerm.VIEW not in perms:
            raise NotFound()
        required = view.project_perms.get(_action_name(view, request))
        if required is None:
            if request.method in SAFE_METHODS:
                return True
            # Workspace-level actions on a project (e.g. delete) are covered by IsWorkspaceMember
            return view.workspace_perms.get(_action_name(view, request)) not in (None, PROJECT_LEVEL)
        return required in perms
