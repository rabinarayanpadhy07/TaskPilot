from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from issues.models import Issue
from sprints.models import Sprint
from tests.factories import Team, client_for, make_issue, make_sprint


class ReportTests(TestCase):
    def setUp(self):
        self.team = Team()
        t = self.team
        today = timezone.localdate()
        self.epic = make_issue(t.public, issue_type=Issue.Type.EPIC, title="Epic")
        make_issue(t.public, parent=self.epic, status=Issue.Status.DONE, story_points=3, assignee=t.dev)
        make_issue(t.public, parent=self.epic, status=Issue.Status.IN_PROGRESS, story_points=5, assignee=t.dev)
        make_issue(t.public, status=Issue.Status.TODO, due_date=today - timedelta(days=2), assignee=t.viewer)
        make_issue(t.private, status=Issue.Status.TODO, due_date=today - timedelta(days=5), title="Private overdue")
        make_issue(t.other_project, status=Issue.Status.DONE)
        sprint = make_sprint(t.public, status=Sprint.Status.COMPLETED, committed_points=10, completed_points=8)
        Sprint.objects.filter(pk=sprint.pk).update(completed_at=timezone.now() - timedelta(days=1))

    def report(self, user, **params):
        return client_for(user).get(self.team.url("reports/"), params)

    def test_every_role_can_view_reports_scoped_to_their_projects(self):
        t = self.team
        dev = self.report(t.dev).json()
        self.assertEqual(dev["projects"], [t.public.pk])
        self.assertEqual(dev["totals"]["overdue"], 1)
        self.assertEqual(dev["totals"]["issues"], 3)  # epics excluded, private excluded
        admin = self.report(t.admin).json()
        self.assertEqual(admin["totals"]["overdue"], 2)
        self.assertEqual(self.report(t.viewer).status_code, 200)

    def test_report_contents(self):
        data = self.report(self.team.dev).json()
        statuses = {row["status"]: row["count"] for row in data["status_distribution"]}
        self.assertEqual(statuses, {"backlog": 0, "todo": 1, "in_progress": 1, "in_review": 0, "done": 1})
        self.assertEqual(data["range"]["granularity"], "day")
        self.assertEqual(sum(b["count"] for b in data["completed_over_time"]), 1)
        self.assertEqual(data["sprint_velocity"][0]["completed_points"], 8)
        epic = data["epic_progress"][0]
        self.assertEqual((epic["total"], epic["done"], epic["points"], epic["done_points"]), (2, 1, 8, 3))
        dev_row = next(row for row in data["workload"] if row["assignee"] and row["assignee"]["id"] == self.team.dev.pk)
        self.assertEqual(dev_row["open"], 1)

    def test_project_filter_cannot_reach_hidden_or_foreign_projects(self):
        t = self.team
        self.assertEqual(self.report(t.dev, project=t.private.pk).status_code, 404)
        self.assertEqual(self.report(t.owner, project=t.other_project.pk).status_code, 404)
        self.assertEqual(self.report(t.admin, project=t.private.pk).json()["totals"]["overdue"], 1)

    def test_date_validation_and_weekly_buckets(self):
        t = self.team
        self.assertEqual(self.report(t.dev, **{"from": "2026-02-10", "to": "2026-01-01"}).status_code, 400)
        self.assertEqual(self.report(t.dev, **{"from": "nope"}).status_code, 400)
        self.assertEqual(self.report(t.dev, **{"from": "2024-01-01", "to": "2026-01-01"}).status_code, 400)
        data = self.report(t.dev, **{"from": "2026-01-01", "to": "2026-03-31"}).json()
        self.assertEqual(data["range"]["granularity"], "week")

    def test_outsider_gets_404(self):
        self.assertEqual(self.report(self.team.outsider).status_code, 404)
