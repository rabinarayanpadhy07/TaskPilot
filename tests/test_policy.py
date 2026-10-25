"""Unit tests for the role matrix and access resolution (no HTTP)."""

from django.test import TestCase

from projects.models import Project
from tests.factories import Team, add_member, make_project, make_user
from workspaces import access
from workspaces.models import WorkspaceMembership
from workspaces.roles import (
    ProjectPerm,
    ProjectRole,
    WorkspacePerm,
    WorkspaceRole,
    assignable_roles,
    can_manage_role,
)


def membership(team, user):
    return WorkspaceMembership.objects.get(workspace=team.ws, user=user)


class RoleMatrixTests(TestCase):
    def test_workspace_permissions_per_role(self):
        team = Team()
        def has(user, perm):
            return access.has_workspace_perm(membership(team, user), perm)

        self.assertTrue(has(team.owner, WorkspacePerm.DELETE))
        self.assertFalse(has(team.admin, WorkspacePerm.DELETE))
        self.assertTrue(has(team.admin, WorkspacePerm.MEMBERS_MANAGE))
        self.assertTrue(has(team.pm, WorkspacePerm.PROJECTS_CREATE))
        self.assertFalse(has(team.pm, WorkspacePerm.MEMBERS_INVITE))
        self.assertFalse(has(team.dev, WorkspacePerm.PROJECTS_CREATE))
        for user in (team.owner, team.admin, team.pm, team.dev, team.viewer):
            self.assertTrue(has(user, WorkspacePerm.REPORTS_VIEW))

    def test_assignable_roles_follow_hierarchy(self):
        self.assertEqual(set(assignable_roles(WorkspaceRole.OWNER)), set(WorkspaceRole))
        self.assertEqual(
            assignable_roles(WorkspaceRole.ADMIN),
            [WorkspaceRole.PROJECT_MANAGER, WorkspaceRole.DEVELOPER, WorkspaceRole.VIEWER],
        )
        self.assertFalse(can_manage_role(WorkspaceRole.ADMIN, WorkspaceRole.ADMIN))
        self.assertFalse(can_manage_role(WorkspaceRole.ADMIN, WorkspaceRole.OWNER))
        self.assertTrue(can_manage_role(WorkspaceRole.OWNER, WorkspaceRole.OWNER))

    def test_member_without_invite_permission_can_assign_nothing(self):
        team = Team()
        self.assertEqual(access.roles_assignable_by(membership(team, team.pm)), [])
        self.assertEqual(access.roles_assignable_by(membership(team, team.dev)), [])


class EffectiveProjectRoleTests(TestCase):
    def setUp(self):
        self.team = Team()

    def role(self, user, project):
        return access.effective_project_role(membership(self.team, user), project)

    def test_workspace_visible_project_defaults(self):
        t = self.team
        self.assertEqual(self.role(t.admin, t.public), ProjectRole.MANAGER)
        self.assertEqual(self.role(t.pm, t.public), ProjectRole.MANAGER)  # explicit membership
        self.assertEqual(self.role(t.dev, t.public), ProjectRole.CONTRIBUTOR)
        self.assertEqual(self.role(t.viewer, t.public), ProjectRole.VIEWER)

    def test_private_project_requires_membership(self):
        t = self.team
        self.assertIsNone(self.role(t.dev, t.private))
        self.assertIsNone(self.role(t.viewer, t.private))
        self.assertEqual(self.role(t.admin, t.private), ProjectRole.MANAGER)
        self.assertEqual(self.role(t.pm, t.private), ProjectRole.MANAGER)

    def test_project_role_is_capped_by_workspace_role(self):
        t = self.team
        capped = make_project(t.ws, key="CAP", visibility=Project.Visibility.PRIVATE, members=[(t.dev, ProjectRole.MANAGER), (t.viewer, ProjectRole.MANAGER)])
        self.assertEqual(self.role(t.dev, capped), ProjectRole.CONTRIBUTOR)
        self.assertEqual(self.role(t.viewer, capped), ProjectRole.VIEWER)
        self.assertFalse(access.has_project_perm(membership(t, t.viewer), capped, ProjectPerm.ISSUES_WRITE))

    def test_never_crosses_workspaces(self):
        t = self.team
        # Even an owner of one workspace has no role in another workspace's project
        self.assertIsNone(self.role(t.owner, t.other_project))
        other_owner = WorkspaceMembership.objects.get(workspace=t.other_ws, user=t.outsider)
        self.assertIsNone(access.effective_project_role(other_owner, t.public))

    def test_visible_projects(self):
        t = self.team
        def visible(user):
            return set(access.visible_projects(membership(t, user)).values_list("key", flat=True))
        self.assertEqual(visible(t.dev), {"PUB"})
        self.assertEqual(visible(t.pm), {"PUB", "PRIV"})
        self.assertEqual(visible(t.admin), {"PUB", "PRIV"})

    def test_get_membership_rejects_garbage_and_inactive_users(self):
        t = self.team
        self.assertIsNone(access.get_membership(t.dev, "abc"))
        self.assertIsNone(access.get_membership(t.dev, t.other_ws.pk))
        inactive = make_user()
        add_member(t.ws, inactive, WorkspaceRole.ADMIN)
        inactive.is_active = False
        inactive.save()
        self.assertIsNone(access.get_membership(inactive, t.ws.pk))
