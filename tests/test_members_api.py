from django.core import mail
from django.test import TestCase
from rest_framework.test import APIClient

from issues.models import Issue
from projects.models import ProjectMembership
from tests.factories import Team, client_for, make_issue, make_user, results
from workspaces.models import Invitation, WorkspaceMembership
from workspaces.roles import ProjectRole, WorkspaceRole


class WorkspaceIsolationTests(TestCase):
    def setUp(self):
        self.team = Team()

    def test_workspace_list_only_contains_own_workspaces(self):
        response = client_for(self.team.dev).get("/api/workspaces/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([w["id"] for w in results(response)], [self.team.ws.pk])
        self.assertEqual(results(response)[0]["role"], "developer")
        self.assertNotIn("members.manage", results(response)[0]["permissions"])

    def test_non_member_gets_404_everywhere_in_a_workspace(self):
        client = client_for(self.team.outsider)
        for path in ("", "members/", "invitations/", "projects/", "issues/", "reports/", f"projects/{self.team.public.pk}/"):
            with self.subTest(path=path):
                url = f"/api/workspaces/{self.team.ws.pk}/{path}"
                self.assertEqual(client.get(url).status_code, 404, url)

    def test_unauthenticated_requests_are_rejected(self):
        response = APIClient().get(self.team.url("members/"))
        self.assertEqual(response.status_code, 401)

    def test_members_of_another_workspace_cannot_be_addressed(self):
        response = client_for(self.team.owner).patch(self.team.url(f"members/{self.team.outsider.pk}/"), {"role": "viewer"}, format="json")
        self.assertEqual(response.status_code, 404)


class MemberRoleTests(TestCase):
    def setUp(self):
        self.team = Team()

    def patch_role(self, actor, target, role, **extra):
        return client_for(actor).patch(self.team.url(f"members/{target.pk}/"), {"role": role, **extra}, format="json")

    def role_of(self, user):
        return WorkspaceMembership.objects.get(workspace=self.team.ws, user=user).role

    def test_member_list_includes_roles_and_per_member_permissions(self):
        response = client_for(self.team.admin).get(self.team.url("members/"))
        rows = {row["email"]: row for row in results(response)}
        self.assertEqual(rows[self.team.dev.email]["role"], "developer")
        self.assertTrue(rows[self.team.dev.email]["permissions"]["can_change_role"])
        self.assertFalse(rows[self.team.owner.email]["permissions"]["can_change_role"])
        self.assertFalse(rows[self.team.admin.email]["permissions"]["can_change_role"])  # self
        self.assertNotIn(self.team.outsider.email, rows)

    def test_developer_cannot_change_roles(self):
        self.assertEqual(self.patch_role(self.team.dev, self.team.viewer, "developer").status_code, 403)
        self.assertEqual(self.role_of(self.team.viewer), WorkspaceRole.VIEWER)

    def test_cannot_change_own_role(self):
        self.assertEqual(self.patch_role(self.team.admin, self.team.admin, "owner").status_code, 403)

    def test_admin_can_manage_lower_roles_only(self):
        t = self.team
        self.assertEqual(self.patch_role(t.admin, t.dev, "project_manager").status_code, 200)
        self.assertEqual(self.role_of(t.dev), WorkspaceRole.PROJECT_MANAGER)
        self.assertEqual(self.patch_role(t.admin, t.dev, "admin").status_code, 403)  # can't grant own level
        self.assertEqual(self.patch_role(t.admin, t.owner, "viewer").status_code, 403)  # can't touch owner
        self.assertEqual(self.role_of(t.owner), WorkspaceRole.OWNER)

    def test_owner_can_promote_to_admin_and_owner(self):
        t = self.team
        self.assertEqual(self.patch_role(t.owner, t.dev, "admin").status_code, 200)
        self.assertEqual(self.patch_role(t.owner, t.admin, "owner").status_code, 200)
        self.assertEqual(self.role_of(t.admin), WorkspaceRole.OWNER)

    def test_invalid_role_is_rejected(self):
        self.assertEqual(self.patch_role(self.team.owner, self.team.dev, "superuser").status_code, 400)

    def test_client_supplied_workspace_is_ignored(self):
        t = self.team
        response = self.patch_role(t.admin, t.dev, "viewer", workspace=t.other_ws.pk)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(WorkspaceMembership.objects.filter(workspace=t.other_ws, user=t.dev).exists())

    def test_last_owner_cannot_leave_or_be_demoted(self):
        t = self.team
        response = client_for(t.owner).delete(t.url(f"members/{t.owner.pk}/"))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.role_of(t.owner), WorkspaceRole.OWNER)

    def test_owner_can_hand_over_and_leave(self):
        t = self.team
        self.patch_role(t.owner, t.admin, "owner")
        self.assertEqual(client_for(t.owner).delete(t.url(f"members/{t.owner.pk}/")).status_code, 204)


class RemoveMemberTests(TestCase):
    def setUp(self):
        self.team = Team()

    def test_admin_removes_developer_and_cleans_up_access(self):
        t = self.team
        ProjectMembership.objects.create(project=t.private, user=t.dev, role=ProjectRole.CONTRIBUTOR)
        open_issue = make_issue(t.public, assignee=t.dev)
        done_issue = make_issue(t.public, status=Issue.Status.DONE, assignee=t.dev)

        response = client_for(t.admin).delete(t.url(f"members/{t.dev.pk}/"))
        self.assertEqual(response.status_code, 204)
        self.assertFalse(WorkspaceMembership.objects.filter(workspace=t.ws, user=t.dev).exists())
        self.assertFalse(ProjectMembership.objects.filter(user=t.dev).exists())
        open_issue.refresh_from_db()
        done_issue.refresh_from_db()
        self.assertIsNone(open_issue.assignee)
        self.assertEqual(done_issue.assignee, t.dev)  # history stays
        # And the removed user immediately loses access
        self.assertEqual(client_for(t.dev).get(t.url("projects/")).status_code, 404)

    def test_admin_cannot_remove_owner_or_other_admin(self):
        t = self.team
        other_admin = make_user()
        WorkspaceMembership.objects.create(workspace=t.ws, user=other_admin, role=WorkspaceRole.ADMIN)
        self.assertEqual(client_for(t.admin).delete(t.url(f"members/{t.owner.pk}/")).status_code, 403)
        self.assertEqual(client_for(t.admin).delete(t.url(f"members/{other_admin.pk}/")).status_code, 403)

    def test_developer_cannot_remove_others_but_can_leave(self):
        t = self.team
        self.assertEqual(client_for(t.dev).delete(t.url(f"members/{t.viewer.pk}/")).status_code, 403)
        self.assertEqual(client_for(t.dev).delete(t.url(f"members/{t.dev.pk}/")).status_code, 204)


class InvitationTests(TestCase):
    def setUp(self):
        self.team = Team()

    def invite(self, actor, email="new@example.com", role="developer"):
        return client_for(actor).post(self.team.url("invitations/"), {"email": email, "role": role}, format="json")

    def test_only_inviters_can_list_or_create(self):
        t = self.team
        for user in (t.pm, t.dev, t.viewer):
            self.assertEqual(client_for(user).get(t.url("invitations/")).status_code, 403)
            self.assertEqual(self.invite(user).status_code, 403)

    def test_admin_invites_within_assignable_roles(self):
        response = self.invite(self.team.admin, "New@Example.com", "developer")
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["email"], "new@example.com")
        self.assertIn("/invite/", body["invite_url"])
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(self.invite(self.team.admin, "x@example.com", "admin").status_code, 403)
        self.assertEqual(self.invite(self.team.admin, "y@example.com", "owner").status_code, 403)

    def test_token_is_not_stored_or_listed(self):
        raw = self.invite(self.team.admin).json()["invite_url"].rsplit("/", 1)[-1]
        invitation = Invitation.objects.get()
        self.assertNotEqual(invitation.token_hash, raw)
        listing = results(client_for(self.team.admin).get(self.team.url("invitations/")))
        self.assertNotIn("invite_url", listing[0])
        self.assertNotIn("token_hash", listing[0])

    def test_existing_member_cannot_be_invited(self):
        self.assertEqual(self.invite(self.team.admin, self.team.dev.email).status_code, 400)

    def test_reinviting_replaces_the_open_invitation(self):
        self.invite(self.team.admin)
        self.invite(self.team.admin, role="viewer")
        self.assertEqual(Invitation.objects.pending().count(), 1)
        self.assertEqual(Invitation.objects.pending().get().role, "viewer")

    def test_accept_flow_requires_matching_email(self):
        t = self.team
        token = self.invite(t.admin, "riley@example.com", "viewer").json()["invite_url"].rsplit("/", 1)[-1]

        public = APIClient().get(f"/api/invitations/{token}/")
        self.assertEqual(public.status_code, 200)
        self.assertEqual(public.json()["workspace"]["name"], "Acme")
        self.assertEqual(APIClient().get("/api/invitations/not-a-token/").status_code, 404)

        wrong = make_user(email="someone@example.com")
        self.assertEqual(client_for(wrong).post(f"/api/invitations/{token}/accept/").status_code, 403)
        self.assertFalse(WorkspaceMembership.objects.filter(workspace=t.ws, user=wrong).exists())

        riley = make_user(email="riley@example.com")
        self.assertEqual(client_for(riley).post(f"/api/invitations/{token}/accept/").status_code, 200)
        self.assertEqual(WorkspaceMembership.objects.get(workspace=t.ws, user=riley).role, WorkspaceRole.VIEWER)
        # Single use
        self.assertEqual(client_for(riley).post(f"/api/invitations/{token}/accept/").status_code, 404)

    def test_revoked_invitation_stops_working(self):
        t = self.team
        body = self.invite(t.admin).json()
        token = body["invite_url"].rsplit("/", 1)[-1]
        self.assertEqual(client_for(t.admin).delete(t.url(f"invitations/{body['id']}/")).status_code, 204)
        self.assertEqual(APIClient().get(f"/api/invitations/{token}/").status_code, 404)

    def test_cannot_revoke_invitations_of_another_workspace(self):
        t = self.team
        other = Invitation.objects.create(
            workspace=t.other_ws, email="z@example.com", role="viewer", token_hash="x" * 64,
            expires_at="2999-01-01T00:00:00Z",
        )
        self.assertEqual(client_for(t.owner).delete(t.url(f"invitations/{other.pk}/")).status_code, 404)
