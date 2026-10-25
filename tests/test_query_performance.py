from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from tests.factories import Team, client_for, make_issue, make_project


class QueryCountTests(TestCase):
    """List endpoints must not run one query per row."""

    def count(self, client, url):
        with CaptureQueriesContext(connection) as ctx:
            response = client.get(url, {"page_size": 200})
        self.assertEqual(response.status_code, 200)
        return len(ctx.captured_queries)

    def test_issue_list_query_count_is_constant(self):
        t = Team()
        client = client_for(t.dev)
        for _ in range(3):
            make_issue(t.public)
        few = self.count(client, t.url("issues/"))
        for _ in range(40):
            make_issue(t.public)
        self.assertEqual(self.count(client, t.url("issues/")), few)

    def test_project_list_query_count_is_constant(self):
        t = Team()
        client = client_for(t.admin)
        few = self.count(client, t.url("projects/"))
        for i in range(15):
            make_issue(make_project(t.ws, key=f"Q{i}"))
        self.assertEqual(self.count(client, t.url("projects/")), few)

    def test_member_list_query_count_is_constant(self):
        t = Team()
        client = client_for(t.admin)
        few = self.count(client, t.url("members/"))
        from tests.factories import add_member, make_user

        for _ in range(20):
            add_member(t.ws, make_user(), "developer")
        self.assertEqual(self.count(client, t.url("members/")), few)
