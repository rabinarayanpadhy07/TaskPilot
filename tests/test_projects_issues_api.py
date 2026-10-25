from django.test import TestCase

from issues.models import Issue
from projects.models import Project, ProjectMembership
from sprints.models import Sprint
from tests.factories import Team, client_for, make_issue, make_sprint, results
from workspaces.roles import ProjectRole


class ProjectAccessTests(TestCase):
    def setUp(self):
        self.team = Team()

    def keys(self, user):
        return {p["key"] for p in results(client_for(user).get(self.team.url("projects/")))}

    def test_private_projects_are_hidden_from_non_members(self):
        t = self.team
        self.assertEqual(self.keys(t.dev), {"PUB"})
        self.assertEqual(self.keys(t.admin), {"PUB", "PRIV"})
        self.assertEqual(client_for(t.dev).get(t.url(f"projects/{t.private.pk}/")).status_code, 404)
        # Another workspace's project id is just as invisible
        self.assertEqual(client_for(t.owner).get(t.url(f"projects/{t.other_project.pk}/")).status_code, 404)

    def test_project_payload_describes_callers_access(self):
        t = self.team
        project = client_for(t.viewer).get(t.url(f"projects/{t.public.pk}/")).json()
        self.assertEqual(project["my_role"], "viewer")
        self.assertEqual(project["permissions"], ["project.view"])

    def test_create_requires_projects_create(self):
        t = self.team
        payload = {"name": "New", "key": "NEW"}
        self.assertEqual(client_for(t.dev).post(t.url("projects/"), payload, format="json").status_code, 403)
        response = client_for(t.pm).post(t.url("projects/"), payload, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["my_role"], "manager")

    def test_create_ignores_spoofed_workspace_and_rejects_foreign_lead(self):
        t = self.team
        response = client_for(t.pm).post(t.url("projects/"), {"name": "X", "key": "XX", "workspace": t.other_ws.pk}, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Project.objects.get(key="XX").workspace, t.ws)
        response = client_for(t.pm).post(t.url("projects/"), {"name": "Y", "key": "YY", "lead": t.outsider.pk}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("lead", response.json())

    def test_update_requires_project_manager_role(self):
        t = self.team
        url = t.url(f"projects/{t.public.pk}/")
        self.assertEqual(client_for(t.dev).patch(url, {"visibility": "private"}, format="json").status_code, 403)
        self.assertEqual(client_for(t.pm).patch(url, {"visibility": "private"}, format="json").status_code, 200)

    def test_delete_is_owner_admin_only(self):
        t = self.team
        url = t.url(f"projects/{t.public.pk}/")
        self.assertEqual(client_for(t.pm).delete(url).status_code, 403)
        self.assertEqual(client_for(t.admin).delete(url).status_code, 204)


class ProjectMemberTests(TestCase):
    def setUp(self):
        self.team = Team()

    def url(self, project, user=None):
        return self.team.url(f"projects/{project.pk}/members/" + (f"{user.pk}/" if user else ""))

    def test_only_project_managers_change_project_members(self):
        t = self.team
        self.assertEqual(client_for(t.dev).post(self.url(t.public), {"user": t.viewer.pk, "role": "viewer"}, format="json").status_code, 403)
        response = client_for(t.pm).post(self.url(t.private), {"user": t.dev.pk, "role": "contributor"}, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["effective_role"], "contributor")
        # Now the developer can see the private project
        self.assertEqual(client_for(t.dev).get(t.url(f"projects/{t.private.pk}/")).status_code, 200)

    def test_role_above_workspace_cap_is_rejected(self):
        t = self.team
        response = client_for(t.pm).post(self.url(t.public), {"user": t.viewer.pk, "role": "manager"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("role", response.json())

    def test_users_outside_the_workspace_cannot_be_added(self):
        t = self.team
        response = client_for(t.pm).post(self.url(t.public), {"user": t.outsider.pk, "role": "viewer"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ProjectMembership.objects.filter(user=t.outsider).exists())

    def test_hidden_project_members_are_not_listed(self):
        t = self.team
        self.assertEqual(client_for(t.dev).get(self.url(t.private)).status_code, 404)

    def test_removing_the_lead_clears_it(self):
        t = self.team
        Project.objects.filter(pk=t.public.pk).update(lead=t.pm)
        ProjectMembership.objects.create(project=t.public, user=t.dev, role=ProjectRole.CONTRIBUTOR)
        self.assertEqual(client_for(t.admin).delete(self.url(t.public, t.pm)).status_code, 204)
        t.public.refresh_from_db()
        self.assertIsNone(t.public.lead)


class IssueAuthorizationTests(TestCase):
    def setUp(self):
        self.team = Team()
        self.issue = make_issue(self.team.public, title="Public issue")
        self.secret = make_issue(self.team.private, title="Private issue")

    def test_list_excludes_hidden_projects_and_other_workspaces(self):
        t = self.team
        titles = {i["title"] for i in results(client_for(t.dev).get(t.url("issues/")))}
        self.assertEqual(titles, {"Public issue"})
        self.assertEqual(client_for(t.dev).get(t.url(f"issues/{self.secret.key}/")).status_code, 404)
        self.assertEqual(client_for(t.owner).get(t.url(f"issues/{t.other_issue.key}/")).status_code, 404)

    def test_viewer_is_read_only(self):
        t = self.team
        client = client_for(t.viewer)
        self.assertEqual(client.get(t.url(f"issues/{self.issue.key}/")).json()["permissions"]["can_edit"], False)
        self.assertEqual(client.patch(t.url(f"issues/{self.issue.key}/"), {"title": "Hacked"}, format="json").status_code, 403)
        self.assertEqual(client.post(t.url("issues/"), {"project": t.public.pk, "title": "New"}, format="json").status_code, 403)

    def test_developer_contributes_but_cannot_delete(self):
        t = self.team
        client = client_for(t.dev)
        self.assertEqual(client.patch(t.url(f"issues/{self.issue.key}/"), {"status": "done"}, format="json").status_code, 200)
        self.issue.refresh_from_db()
        self.assertIsNotNone(self.issue.completed_at)
        created = client.post(t.url("issues/"), {"project": t.public.pk, "title": "New"}, format="json")
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()["key"], f"PUB-{t.public.issue_counter + 1}")
        self.assertEqual(client.delete(t.url(f"issues/{self.issue.key}/")).status_code, 403)
        self.assertEqual(client_for(t.pm).delete(t.url(f"issues/{self.issue.key}/")).status_code, 204)

    def test_cannot_create_in_hidden_or_foreign_projects(self):
        t = self.team
        client = client_for(t.dev)
        for project in (t.private, t.other_project):
            response = client.post(t.url("issues/"), {"project": project.pk, "title": "Sneaky"}, format="json")
            self.assertEqual(response.status_code, 400)
            self.assertIn("project", response.json())
        self.assertFalse(Issue.objects.filter(title="Sneaky").exists())

    def test_references_must_stay_inside_the_workspace(self):
        t = self.team
        url = t.url(f"issues/{self.issue.key}/")
        client = client_for(t.admin)
        self.assertEqual(client.patch(url, {"assignee": t.outsider.pk}, format="json").status_code, 400)
        self.assertEqual(client.patch(url, {"parent": t.other_issue.pk}, format="json").status_code, 400)
        foreign_sprint = make_sprint(t.other_project)
        self.assertEqual(client.patch(url, {"sprint": foreign_sprint.pk}, format="json").status_code, 400)

    def test_sprint_management_requires_manager(self):
        t = self.team
        make_issue(t.public, sprint=None)
        sprint = make_sprint(t.public)
        Issue.objects.filter(pk=self.issue.pk).update(sprint=sprint, story_points=5)
        url = t.url(f"sprints/{sprint.pk}/start/")
        dates = {"start_date": str(sprint.start_date), "end_date": str(sprint.end_date)}
        self.assertEqual(client_for(t.dev).post(url, dates, format="json").status_code, 403)
        response = client_for(t.pm).post(url, dates, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["committed_points"], 5)
        self.assertEqual(client_for(t.dev).post(t.url(f"projects/{t.public.pk}/sprints/"), {"name": "S"}, format="json").status_code, 403)
        self.assertEqual(Sprint.objects.get(pk=sprint.pk).status, "active")
