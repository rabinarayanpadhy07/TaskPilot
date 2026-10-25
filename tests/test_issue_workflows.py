from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from activity.models import Activity
from issues.models import Attachment, Comment, Issue
from notifications.models import Notification
from projects.models import ProjectMembership
from sprints.models import Sprint
from tests.factories import Team, client_for, make_issue, make_sprint, results
from workspaces.roles import ProjectRole

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class IssueLifecycleTests(TestCase):
    def setUp(self):
        self.team = Team()
        self.dev = client_for(self.team.dev)

    def create(self, client=None, **data):
        payload = {"project": self.team.public.pk, "title": "Write docs", **data}
        return (client or self.dev).post(self.team.url("issues/"), payload, format="json")

    def test_create_allocates_keys_records_activity_and_notifies_assignee(self):
        first = self.create(assignee=self.team.pm.pk).json()
        second = self.create().json()
        self.assertEqual((first["key"], second["key"]), ("PUB-1", "PUB-2"))
        self.assertEqual(first["reporter"], self.team.dev.pk)
        issue = Issue.objects.get(key="PUB-1")
        self.assertTrue(Activity.objects.filter(issue=issue, kind="created", actor=self.team.dev).exists())
        note = Notification.objects.get(recipient=self.team.pm)
        self.assertEqual((note.category, note.summary, note.issue_id), ("assignment", "assigned you to", issue.pk))
        # Assigning yourself doesn't notify you
        self.create(assignee=self.team.dev.pk)
        self.assertFalse(Notification.objects.filter(recipient=self.team.dev).exists())

    def test_validation(self):
        self.assertEqual(self.create(title="").status_code, 400)
        self.assertIn("parent", self.create(issue_type="subtask").json())
        epic = make_issue(self.team.public, issue_type="epic")
        self.assertEqual(self.create(issue_type="epic", parent=epic.pk).status_code, 400)
        self.assertEqual(self.create(story_points=101).status_code, 400)
        other_epic = make_issue(self.team.private, issue_type="epic")
        self.assertEqual(self.create(parent=other_epic.pk).status_code, 400)  # hidden + other project

    def test_status_changes_track_completion_activity_and_watchers(self):
        key = self.create(assignee=self.team.dev.pk).json()["key"]
        url = self.team.url(f"issues/{key}/")
        pm = client_for(self.team.pm)
        done = pm.patch(url, {"status": "done"}, format="json").json()
        self.assertIsNotNone(done["completed_at"])
        entry = Activity.objects.filter(field="status").latest("id")
        self.assertEqual((entry.from_value, entry.to_value, entry.actor), ("todo", "done", self.team.pm))
        # reporter/assignee (dev) hears about it; the actor doesn't
        self.assertTrue(Notification.objects.filter(recipient=self.team.dev, category="update", summary="moved to Done").exists())
        self.assertFalse(Notification.objects.filter(recipient=self.team.pm, category="update").exists())
        reopened = pm.patch(url, {"status": "in_progress"}, format="json").json()
        self.assertIsNone(reopened["completed_at"])

    def test_labels_are_normalized(self):
        issue = self.create(labels=[" Bug ", "bug", "UI"]).json()
        self.assertEqual(issue["labels"], ["bug", "ui"])
        self.assertEqual(results(self.dev.get(self.team.url("labels/"))), ["bug", "ui"])

    def test_type_change_drops_sprint(self):
        sprint = make_sprint(self.team.public)
        key = self.create(sprint=sprint.pk).json()["key"]
        epic = self.dev.patch(self.team.url(f"issues/{key}/"), {"issue_type": "epic"}, format="json").json()
        self.assertIsNone(epic["sprint"])

    def test_filters_and_pagination(self):
        t = self.team
        today = timezone.localdate()
        for i in range(7):
            make_issue(t.public, title=f"Bulk {i}", assignee=t.dev if i % 2 else None, due_date=today + timedelta(days=i), labels=["x"] if i < 2 else [])
        make_issue(t.public, status=Issue.Status.DONE, title="Finished")
        url = t.url("issues/")
        page = self.dev.get(url, {"page_size": 3}).json()
        self.assertEqual((page["count"], len(page["results"])), (8, 3))
        self.assertIsNotNone(page["next"])
        self.assertEqual(len(results(self.dev.get(url, {"assignee": t.dev.pk}))), 3)
        self.assertEqual(len(results(self.dev.get(url, {"assignee": "none"}))), 5)
        self.assertEqual(len(results(self.dev.get(url, {"open": "true", "due_before": str(today + timedelta(days=2))}))), 3)
        self.assertEqual(len(results(self.dev.get(url, {"status": ["done"]}))), 1)
        self.assertEqual(len(results(self.dev.get(url, {"label": "X"}))), 2)
        self.assertEqual(len(results(self.dev.get(url, {"q": "bulk 3"}))), 1)
        self.assertEqual(len(results(self.dev.get(url, {"project": "abc"}))), 0)

    def test_delete_removes_subtasks_and_keeps_epic_children(self):
        t = self.team
        epic = make_issue(t.public, issue_type="epic")
        story = make_issue(t.public, issue_type="story", parent=epic)
        sub = make_issue(t.public, issue_type="subtask", parent=story)
        pm = client_for(t.pm)
        self.assertEqual(pm.delete(t.url(f"issues/{story.key}/")).status_code, 204)
        self.assertFalse(Issue.objects.filter(pk=sub.pk).exists())
        child = make_issue(t.public, issue_type="task", parent=epic)
        self.assertEqual(pm.delete(t.url(f"issues/{epic.key}/")).status_code, 204)
        child.refresh_from_db()
        self.assertIsNone(child.parent)


class MoveAndBulkTests(TestCase):
    def setUp(self):
        self.team = Team()
        t = self.team
        self.a, self.b, self.c = (make_issue(t.public, title=n, rank=r) for n, r in (("A", 1000), ("B", 2000), ("C", 3000)))
        self.dev = client_for(t.dev)

    def move(self, issue, client=None, **data):
        return (client or self.dev).post(self.team.url(f"issues/{issue.key}/move/"), data, format="json")

    def test_move_between_neighbours_and_change_status(self):
        response = self.move(self.c, before=self.a.key, after=self.b.key, status="in_progress")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["rank"], 1500)
        self.assertEqual(response.json()["status"], "in_progress")
        self.assertEqual(self.move(self.a, before=self.c.key).json()["rank"], 2500)
        self.assertEqual(self.move(self.b, after=self.c.key).json()["rank"], 500)

    def test_ranks_are_renumbered_when_gap_runs_out(self):
        Issue.objects.filter(pk=self.b.pk).update(rank=1000.0000000001)
        response = self.move(self.c, before=self.a.key, after=self.b.key)
        self.assertEqual(response.status_code, 200)
        ranks = list(Issue.objects.filter(project=self.team.public).order_by("rank").values_list("title", flat=True))
        self.assertEqual(ranks, ["A", "C", "B"])

    def test_move_validation_and_permissions(self):
        t = self.team
        self.assertEqual(self.move(self.a, before="PUB-999").status_code, 400)
        foreign = make_issue(t.private, title="secret")
        self.assertEqual(self.move(self.a, before=foreign.key).status_code, 400)
        done_sprint = make_sprint(t.public, status=Sprint.Status.COMPLETED, start_date=None, end_date=None)
        self.assertEqual(self.move(self.a, sprint=done_sprint.pk).status_code, 400)
        self.assertEqual(self.move(self.a, client=client_for(t.viewer), status="done").status_code, 403)
        self.assertEqual(self.move(self.a, client=client_for(t.outsider), status="done").status_code, 404)

    def test_bulk_is_all_or_nothing(self):
        t = self.team
        epic = make_issue(t.public, issue_type="epic", title="E")
        sprint = make_sprint(t.public)
        url = t.url("issues/bulk/")
        bad = self.dev.post(url, {"keys": [self.a.key, epic.key], "changes": {"sprint": sprint.pk}}, format="json")
        self.assertEqual(bad.status_code, 400)
        self.assertIn(epic.key, bad.json()["detail"])
        self.a.refresh_from_db()
        self.assertIsNone(self.a.sprint)
        ok = self.dev.post(url, {"keys": [self.a.key, self.b.key], "changes": {"priority": "highest", "status": "done"}}, format="json")
        self.assertEqual(ok.status_code, 200)
        self.assertEqual({i["priority"] for i in ok.json()}, {"highest"})
        self.assertTrue(all(i["completed_at"] for i in ok.json()))
        self.assertEqual(self.dev.post(url, {"keys": [self.a.key], "changes": {"title": "x"}}, format="json").status_code, 400)
        viewer = client_for(t.viewer).post(url, {"keys": [self.a.key], "changes": {"priority": "low"}}, format="json")
        self.assertEqual(viewer.status_code, 403)
        hidden = make_issue(t.private)
        self.assertEqual(self.dev.post(url, {"keys": [hidden.key], "changes": {"priority": "low"}}, format="json").status_code, 404)


class CommentTests(TestCase):
    def setUp(self):
        self.team = Team()
        self.issue = make_issue(self.team.public, reporter=self.team.pm, assignee=self.team.viewer)
        self.url = self.team.url(f"issues/{self.issue.key}/comments/")

    def test_comment_mentions_and_follower_notifications(self):
        t = self.team
        response = client_for(t.dev).post(self.url, {"body": "Thanks @pat, see @nobody"}, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["permissions"], {"can_edit": True, "can_delete": True})
        self.assertTrue(Notification.objects.filter(recipient=t.pm, category="mention").exists())
        # assignee (viewer) is a follower; the mentioned PM doesn't get a duplicate comment notification
        self.assertTrue(Notification.objects.filter(recipient=t.viewer, category="comment").exists())
        self.assertFalse(Notification.objects.filter(recipient=t.pm, category="comment").exists())
        self.assertTrue(Activity.objects.filter(issue=self.issue, kind="commented").exists())

    def test_mentions_never_leak_private_projects(self):
        t = self.team
        secret = make_issue(t.private)
        client_for(t.pm).post(t.url(f"issues/{secret.key}/comments/"), {"body": "@dev look at this"}, format="json")
        self.assertFalse(Notification.objects.filter(recipient=t.dev).exists())

    def test_permissions(self):
        t = self.team
        self.assertEqual(client_for(t.viewer).post(self.url, {"body": "hi"}, format="json").status_code, 403)
        self.assertEqual(client_for(t.dev).post(self.url, {"body": "   "}, format="json").status_code, 400)
        comment = Comment.objects.create(issue=self.issue, author=t.dev, body="mine")
        detail = f"{self.url}{comment.pk}/"
        self.assertEqual(client_for(t.admin).patch(detail, {"body": "edited by admin"}, format="json").status_code, 403)
        edited = client_for(t.dev).patch(detail, {"body": "edited"}, format="json").json()
        self.assertIsNotNone(edited["edited_at"])
        other = Comment.objects.create(issue=self.issue, author=t.admin, body="theirs")
        self.assertEqual(client_for(t.dev).delete(f"{self.url}{other.pk}/").status_code, 403)
        self.assertEqual(client_for(t.pm).delete(f"{self.url}{other.pk}/").status_code, 204)  # project manager
        # A comment id from another issue can't be reached through this one
        elsewhere = Comment.objects.create(issue=make_issue(t.public), author=t.dev, body="x")
        self.assertEqual(client_for(t.dev).delete(f"{self.url}{elsewhere.pk}/").status_code, 404)

    def test_hidden_issue_comments_are_404(self):
        t = self.team
        secret = make_issue(t.private)
        self.assertEqual(client_for(t.dev).get(t.url(f"issues/{secret.key}/comments/")).status_code, 404)


class ActivityAndNotificationTests(TestCase):
    def setUp(self):
        self.team = Team()

    def test_workspace_activity_respects_project_visibility(self):
        t = self.team
        public = make_issue(t.public)
        secret = make_issue(t.private)
        from activity import services

        services.record(public, t.pm, "created")
        services.record(secret, t.pm, "created")
        keys = [e["issue_key"] for e in results(client_for(t.dev).get(t.url("activity/")))]
        self.assertEqual(keys, [public.key])
        self.assertEqual(len(results(client_for(t.admin).get(t.url("activity/")))), 2)
        self.assertEqual(client_for(t.dev).get(t.url(f"issues/{secret.key}/activity/")).status_code, 404)

    def test_notifications_are_private_to_recipient(self):
        t = self.team
        issue = make_issue(t.public)
        mine = Notification.objects.create(recipient=t.dev, workspace=t.ws, category="update", summary="x", issue=issue, project=t.public)
        theirs = Notification.objects.create(recipient=t.pm, workspace=t.ws, category="update", summary="y", issue=issue, project=t.public)
        dev = client_for(t.dev)
        rows = results(dev.get("/api/notifications/", {"workspace": t.ws.pk}))
        self.assertEqual([n["id"] for n in rows], [mine.pk])
        self.assertEqual(dev.get("/api/notifications/", {"workspace": t.other_ws.pk}).status_code, 404)
        self.assertEqual(dev.post(f"/api/notifications/{theirs.pk}/mark-unread/").status_code, 404)
        dev.post("/api/notifications/mark-read/", {"workspace": t.ws.pk, "ids": None}, format="json")
        mine.refresh_from_db()
        theirs.refresh_from_db()
        self.assertTrue(mine.read)
        self.assertFalse(theirs.read)

    def test_notifications_hidden_after_losing_project_access(self):
        t = self.team
        ProjectMembership.objects.create(project=t.private, user=t.dev, role=ProjectRole.CONTRIBUTOR)
        issue = make_issue(t.private)
        Notification.objects.create(recipient=t.dev, workspace=t.ws, category="update", summary="x", issue=issue, project=t.private)
        self.assertEqual(len(results(client_for(t.dev).get("/api/notifications/", {"workspace": t.ws.pk}))), 1)
        ProjectMembership.objects.filter(user=t.dev).delete()
        self.assertEqual(len(results(client_for(t.dev).get("/api/notifications/", {"workspace": t.ws.pk}))), 0)


@override_settings(ATTACHMENT_MAX_BYTES=1024)
class AttachmentTests(TestCase):
    def setUp(self):
        self.team = Team()
        self.issue = make_issue(self.team.public)
        self.url = self.team.url(f"issues/{self.issue.key}/attachments/")

    def upload(self, user, name="shot.png", content=PNG, content_type="image/png"):
        return client_for(user).post(self.url, {"file": SimpleUploadedFile(name, content, content_type=content_type)}, format="multipart")

    def test_upload_download_and_access_checks(self):
        t = self.team
        response = self.upload(t.dev)
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual((body["file_name"], body["size"]), ("shot.png", len(PNG)))
        download = APIClient().get(body["url"])  # signed: no auth header needed
        self.assertEqual(download.status_code, 200)
        self.assertIn("attachment", download["Content-Disposition"])
        self.assertEqual(download["X-Content-Type-Options"], "nosniff")
        self.assertEqual(b"".join(download.streaming_content), PNG)
        self.assertEqual(APIClient().get(body["url"][:-3] + "xyz/").status_code, 404)  # tampered
        self.assertTrue(Activity.objects.filter(issue=self.issue, kind="attachment_added").exists())
        # Removing the member invalidates links issued to them
        client_for(t.admin).delete(t.url(f"members/{t.dev.pk}/"))
        self.assertEqual(APIClient().get(body["url"]).status_code, 404)

    @override_settings(ATTACHMENT_URL_MAX_AGE=-1)
    def test_links_expire(self):
        url = self.upload(self.team.dev).json()["url"]
        self.assertEqual(APIClient().get(url).status_code, 404)

    def test_rejected_uploads(self):
        t = self.team
        self.assertEqual(self.upload(t.viewer).status_code, 403)
        self.assertEqual(self.upload(t.dev, "page.html", b"<script>", "text/html").status_code, 400)
        self.assertEqual(self.upload(t.dev, "logo.svg", b"<svg/>", "image/svg+xml").status_code, 400)
        self.assertEqual(self.upload(t.dev, "fake.png", b"x", "text/html").status_code, 400)
        self.assertEqual(self.upload(t.dev, "big.png", PNG * 20).status_code, 400)
        self.assertEqual(Attachment.objects.count(), 0)

    def test_delete_rules(self):
        t = self.team
        mine = self.upload(t.dev).json()["id"]
        theirs = self.upload(t.pm).json()["id"]
        self.assertEqual(client_for(t.dev).delete(f"{self.url}{theirs}/").status_code, 403)
        self.assertEqual(client_for(t.dev).delete(f"{self.url}{mine}/").status_code, 204)
        self.assertEqual(client_for(t.pm).delete(f"{self.url}{theirs}/").status_code, 204)


class SprintWorkflowTests(TestCase):
    def setUp(self):
        self.team = Team()
        self.pm = client_for(self.team.pm)

    def test_complete_moves_open_issues_and_records_history(self):
        t = self.team
        sprint = make_sprint(t.public, status=Sprint.Status.ACTIVE)
        nxt = make_sprint(t.public, name="Next", start_date=timezone.localdate() + timedelta(days=20), end_date=timezone.localdate() + timedelta(days=30))
        open_issue = make_issue(t.public, sprint=sprint, story_points=3)
        done_issue = make_issue(t.public, sprint=sprint, status=Issue.Status.DONE, story_points=5)
        response = self.pm.post(t.url(f"sprints/{sprint.pk}/complete/"), {"move_open_issues_to": nxt.pk}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.json()["status"], response.json()["completed_points"]), ("completed", 5))
        open_issue.refresh_from_db()
        done_issue.refresh_from_db()
        self.assertEqual((open_issue.sprint_id, done_issue.sprint_id), (nxt.pk, sprint.pk))
        self.assertTrue(Activity.objects.filter(issue=open_issue, field="sprint", from_value=sprint.pk, to_value=nxt.pk).exists())
        self.assertEqual(self.pm.patch(t.url(f"sprints/{sprint.pk}/"), {"name": "x"}, format="json").status_code, 400)

    def test_only_one_active_sprint_and_validation(self):
        t = self.team
        make_sprint(t.public, status=Sprint.Status.ACTIVE)
        today = timezone.localdate()
        other = make_sprint(t.public, name="Other", start_date=today + timedelta(days=40), end_date=today + timedelta(days=50))
        make_issue(t.public, sprint=other)
        dates = {"start_date": str(other.start_date), "end_date": str(other.end_date)}
        self.assertEqual(self.pm.post(t.url(f"sprints/{other.pk}/start/"), dates, format="json").status_code, 400)
        overlap = self.pm.post(t.url(f"projects/{t.public.pk}/sprints/"), {"name": "Clash", "start_date": str(today), "end_date": str(today + timedelta(days=3))}, format="json")
        self.assertEqual(overlap.status_code, 400)
        backwards = self.pm.post(t.url(f"projects/{t.public.pk}/sprints/"), {"name": "Bad", "start_date": str(today + timedelta(days=90)), "end_date": str(today + timedelta(days=80))}, format="json")
        self.assertEqual(backwards.status_code, 400)
        listing = results(client_for(t.viewer).get(t.url(f"projects/{t.public.pk}/sprints/")))
        self.assertEqual(listing[0]["status"], "active")


class SearchTests(TestCase):
    def test_search_only_returns_visible_things(self):
        t = Team()
        make_issue(t.public, title="Payment page")
        make_issue(t.private, title="Payment secrets")
        make_issue(t.other_project, title="Payment elsewhere")
        data = client_for(t.dev).get(t.url("search/"), {"q": "payment"}).json()
        self.assertEqual([i["title"] for i in data["issues"]], ["Payment page"])
        self.assertEqual(client_for(t.dev).get(t.url("search/"), {"q": "p"}).json()["issues"], [])
        people = client_for(t.dev).get(t.url("search/"), {"q": "viewer"}).json()["members"]
        self.assertEqual([m["email"] for m in people], [t.viewer.email])
        self.assertEqual(client_for(t.outsider).get(t.url("search/"), {"q": "payment"}).status_code, 404)


class DatabaseConstraintTests(TestCase):
    """Rules that hold even if a code path forgets them."""

    def test_constraints(self):
        t = Team()
        issue = make_issue(t.public)
        with transaction.atomic(), self.assertRaises(IntegrityError):
            Issue.objects.filter(pk=issue.pk).update(status="done")  # done without completed_at
        epic = make_issue(t.public, issue_type="epic")
        sprint = make_sprint(t.public)
        with transaction.atomic(), self.assertRaises(IntegrityError):
            Issue.objects.filter(pk=epic.pk).update(sprint=sprint)
        make_sprint(t.public, status=Sprint.Status.ACTIVE)
        with transaction.atomic(), self.assertRaises(IntegrityError):
            make_sprint(t.public, status=Sprint.Status.ACTIVE)
        with transaction.atomic(), self.assertRaises(IntegrityError):
            Issue.objects.filter(pk=issue.pk).update(status="nope")
