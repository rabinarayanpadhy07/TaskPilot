"""
Development seed data: two demo workspaces with members in every role,
projects (one private), sprints, issues, comments, activity and notifications.

    python manage.py seed_dev                  # create if missing
    python manage.py seed_dev --reset          # delete and recreate the demo workspaces
    python manage.py seed_dev --password ...   # demo password (else SEED_PASSWORD or a random one, printed once)

Safety:
- Refuses to run when DJANGO_ENV=production or DEBUG is off.
- Only touches the demo workspaces (slugs below) and demo users (@app.com
  addresses created here). Existing data is never modified.
"""

import secrets
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from activity import services as activity
from activity.models import Activity
from issues.models import Comment, Issue
from notifications.models import Notification
from projects.models import Project, ProjectMembership
from sprints.models import Sprint
from workspaces.models import Invitation, Team, Workspace, WorkspaceMembership

DEMO_SLUGS = ("demo-acme", "demo-northwind")

PEOPLE = {
    "silu": ("Silu", "Product Manager"),
    "priya": ("Priya Shah", "Product Designer"),
    "jordan": ("Jordan Lee", "Frontend Engineer"),
    "kuresu": ("Kuresu", "Backend Engineer"),
    "srikanta": ("Srikanta", "Engineering Manager"),
    "diego": ("Diego Alvarez", "Marketing Lead"),
    "riley": ("Riley Chen", "Client stakeholder"),
}

WORKSPACES = [
    {
        "slug": "demo-acme", "name": "Acme Inc",
        "members": {"srikanta": "owner", "silu": "admin", "diego": "project_manager", "priya": "developer",
                    "jordan": "developer", "kuresu": "developer", "riley": "viewer"},
        "teams": {"Design": ["priya", "silu"], "Engineering": ["jordan", "kuresu", "srikanta"], "Marketing": ["diego", "silu"]},
        "invitations": [("noah@app.com", "developer", "silu"), ("lena@app.com", "viewer", "srikanta")],
        "projects": [
            {"key": "WEB", "name": "Website redesign", "status": "on_track", "visibility": "workspace", "lead": "silu",
             "description": "Refresh the marketing site and pricing experience ahead of the Q4 launch.",
             "members": {"silu": "manager", "priya": "contributor", "jordan": "contributor", "riley": "viewer"}},
            {"key": "MOB", "name": "Mobile app", "status": "at_risk", "visibility": "workspace", "lead": "srikanta",
             "description": "iOS and Android apps for tracking work on the go, with offline support.",
             "members": {"srikanta": "manager", "jordan": "contributor", "kuresu": "contributor", "silu": "contributor", "priya": "contributor"}},
            {"key": "API", "name": "Platform API", "status": "on_track", "visibility": "private", "lead": "kuresu",
             "description": "Public REST API, authentication and webhooks for integrations.",
             "members": {"kuresu": "manager", "srikanta": "manager", "silu": "contributor"}},
            {"key": "MKT", "name": "Q4 launch campaign", "status": "planning", "visibility": "workspace", "lead": "diego",
             "description": "Launch messaging, email sequences, webinar and partner outreach.",
             "members": {"diego": "manager", "silu": "contributor", "priya": "contributor"}},
        ],
    },
    {
        "slug": "demo-northwind", "name": "Northwind Labs",
        "members": {"silu": "owner", "priya": "admin", "kuresu": "developer"},
        "teams": {"Research": ["silu", "priya"]},
        "invitations": [],
        "projects": [
            {"key": "RES", "name": "Research portal", "status": "on_track", "visibility": "workspace", "lead": "priya",
             "description": "Central library for interview notes, transcripts and insights.",
             "members": {"priya": "manager", "silu": "contributor"}},
        ],
    },
]

# (key, project sprint name, start offset, end offset, status, committed, completed)
SPRINTS = [
    ("WEB", "WEB Sprint 1", -32, -18, "completed", 13, 9),
    ("WEB", "WEB Sprint 2", -4, 10, "active", None, None),
    ("WEB", "WEB Sprint 3", 11, 25, "planned", None, None),
    ("MOB", "MOB Sprint 6", -23, -10, "completed", 18, 13),
    ("MOB", "MOB Sprint 7", -9, 5, "active", None, None),
    ("API", "API Sprint 3", -30, -17, "completed", 16, 8),
    ("API", "API Sprint 4", -2, 12, "active", None, None),
]

# key-suffix, type, title, status, priority, assignee, reporter, extras
ISSUES = [
    ("WEB", "epic", "Marketing site refresh", "in_progress", "high", "silu", "silu", {"due": 21}),
    ("WEB", "epic", "Pricing & plans experience", "todo", "medium", "priya", "silu", {"due": 35}),
    ("WEB", "story", "New homepage hero and value props", "in_review", "high", "priya", "silu", {"parent": 1, "due": 2, "points": 5, "sprint": "WEB Sprint 2", "labels": ["design", "homepage"]}),
    ("WEB", "task", "Audit existing pages for outdated screenshots", "done", "low", "jordan", "silu", {"parent": 1, "points": 2, "sprint": "WEB Sprint 2", "completed": 3}),
    ("WEB", "bug", "Mobile nav overlaps logo on Safari", "in_progress", "highest", "jordan", "priya", {"parent": 1, "due": -1, "points": 3, "sprint": "WEB Sprint 2", "labels": ["mobile"]}),
    ("WEB", "story", "Customer logos carousel", "todo", "medium", "jordan", "silu", {"parent": 1, "due": 6, "points": 3, "sprint": "WEB Sprint 2"}),
    ("WEB", "subtask", "Collect logo approvals from partners", "todo", "low", "silu", "silu", {"parent": 6, "due": 4}),
    ("WEB", "story", "Update pricing page layout", "in_progress", "high", "silu", "priya", {"parent": 2, "due": 3, "points": 5, "sprint": "WEB Sprint 2", "labels": ["pricing"]}),
    ("WEB", "task", "Write FAQ copy for annual billing", "todo", "medium", "silu", "priya", {"parent": 2, "due": -2, "sprint": "WEB Sprint 2", "labels": ["copy"]}),
    ("WEB", "bug", "Plan comparison table misaligned at 1024px", "done", "medium", "jordan", "priya", {"parent": 2, "points": 1, "sprint": "WEB Sprint 1", "completed": 20}),
    ("WEB", "task", "Set up analytics events for CTA clicks", "backlog", "low", None, "silu", {"parent": 1, "labels": ["analytics"]}),
    ("WEB", "story", "Agree on new information architecture", "done", "high", "priya", "silu", {"parent": 1, "points": 5, "sprint": "WEB Sprint 1", "completed": 19}),
    ("MOB", "epic", "Offline mode", "in_progress", "high", "srikanta", "srikanta", {"due": 30}),
    ("MOB", "story", "Cache project list for offline use", "done", "high", "jordan", "srikanta", {"parent": 1, "points": 8, "sprint": "MOB Sprint 6", "completed": 12}),
    ("MOB", "story", "Queue edits made offline", "in_progress", "highest", "jordan", "srikanta", {"parent": 1, "due": 5, "points": 8, "sprint": "MOB Sprint 7"}),
    ("MOB", "bug", "Offline sync conflicts duplicate comments", "in_progress", "highest", "silu", "srikanta", {"parent": 1, "due": 0, "points": 5, "sprint": "MOB Sprint 7", "labels": ["sync"]}),
    ("MOB", "bug", "App crashes when opening attachments over 20 MB", "todo", "high", "jordan", "kuresu", {"due": -4, "points": 3, "sprint": "MOB Sprint 7"}),
    ("MOB", "task", "Prepare TestFlight build for beta group", "done", "medium", "srikanta", "srikanta", {"sprint": "MOB Sprint 7", "completed": 1, "points": 2}),
    ("API", "epic", "Public REST API v1", "in_progress", "high", "kuresu", "srikanta", {"due": 20}),
    ("API", "story", "Rate limiting for webhooks", "in_review", "high", "kuresu", "srikanta", {"parent": 1, "due": 1, "points": 5, "sprint": "API Sprint 4"}),
    ("API", "task", "Publish OpenAPI schema", "in_progress", "medium", "silu", "kuresu", {"parent": 1, "due": 7, "sprint": "API Sprint 4", "points": 3}),
    ("API", "bug", "Pagination cursor skips items with equal timestamps", "todo", "high", "kuresu", "silu", {"parent": 1, "due": 3, "points": 3, "sprint": "API Sprint 4"}),
    ("API", "story", "API keys management screen", "done", "high", "kuresu", "srikanta", {"parent": 1, "points": 8, "sprint": "API Sprint 3", "completed": 18}),
    ("MKT", "epic", "Q4 launch", "todo", "high", "diego", "diego", {"due": 40}),
    ("MKT", "task", "Launch email copy", "in_review", "medium", "diego", "diego", {"parent": 1, "due": 2, "labels": ["copy"]}),
    ("MKT", "story", "Launch landing page", "todo", "high", "priya", "diego", {"parent": 1, "due": 14, "points": 5}),
    ("MKT", "task", "Book launch webinar slot", "done", "low", "diego", "diego", {"parent": 1, "completed": 4}),
    ("RES", "epic", "Insights library", "in_progress", "medium", "silu", "priya", {"due": 25}),
    ("RES", "task", "Tag interview transcripts", "todo", "medium", "silu", "priya", {"parent": 1, "due": 2}),
    ("RES", "bug", "Transcript upload fails for .m4a files", "in_progress", "high", "silu", "priya", {"due": -1, "points": 2}),
]

COMMENTS = [
    ("WEB-8", "priya", "Designs for the annual toggle are final. The badge should say “2 months free”."),
    ("WEB-8", "silu", "Thanks! I'll update the copy and get the enterprise row reviewed by sales."),
    ("MOB-4", "srikanta", "@Silu can you own the conflict rules? Kuresu will pick up the integration tests."),
    ("MOB-4", "kuresu", "Reproduced on a Pixel 8. The retry queue re-sends after the server already accepted it."),
    ("API-2", "srikanta", "Let's default to 60 requests per minute per endpoint, configurable per integration."),
]


class Command(BaseCommand):
    help = "Create demo workspaces for local development (never in production)."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Delete and recreate the demo workspaces.")
        parser.add_argument("--password", help="Password for the demo users (default: SEED_PASSWORD or random).")

    def handle(self, *args, reset=False, password=None, **options):
        if getattr(settings, "IS_PRODUCTION", True) or not settings.DEBUG:
            raise CommandError("seed_dev only runs in development (DJANGO_ENV=development with DEBUG on).")

        existing = Workspace.objects.filter(slug__in=DEMO_SLUGS)
        if existing.exists() and not reset:
            self.stdout.write("Demo workspaces already exist. Use --reset to recreate them.")
            return

        password = password or settings_password() or secrets.token_urlsafe(12)
        with transaction.atomic():
            existing.delete()
            users = self._users(password)
            for spec in WORKSPACES:
                self._workspace(spec, users)

        self.stdout.write(self.style.SUCCESS("Demo data ready."))
        self.stdout.write(f"Sign in as silu@app.com (Admin in Acme, Owner in Northwind) or any of: "
                          f"{', '.join(f'{k}@app.com' for k in PEOPLE)}")
        self.stdout.write(f"Password for all demo users: {password}")

    def _users(self, password):
        users = {}
        for handle, (name, title) in PEOPLE.items():
            email = f"{handle}@app.com"
            user = User.objects.filter(email=email).first()
            if user is None:
                user = User.objects.create_user(email=email, password=password, full_name=name, title=title)
            elif not user.has_usable_password() or not user.check_password(password):
                user.set_password(password)
                user.save(update_fields=["password"])
            users[handle] = user
        return users

    def _workspace(self, spec, users):
        now = timezone.now()
        today = timezone.localdate()
        ws = Workspace.objects.create(name=spec["name"], slug=spec["slug"], created_by=users[next(iter(spec["members"]))])
        for handle, role in spec["members"].items():
            WorkspaceMembership.objects.create(workspace=ws, user=users[handle], role=role)
        for name, handles in spec["teams"].items():
            Team.objects.create(workspace=ws, name=name).members.set([users[h] for h in handles])
        for email, role, inviter in spec["invitations"]:
            _raw, token_hash = Invitation.new_token()
            Invitation.objects.create(workspace=ws, email=email, role=role, invited_by=users[inviter], token_hash=token_hash,
                                      expires_at=now + settings.INVITATION_TTL)

        projects = {}
        for p in spec["projects"]:
            project = Project.objects.create(workspace=ws, key=p["key"], name=p["name"], description=p["description"],
                                             status=p["status"], visibility=p["visibility"], lead=users[p["lead"]])
            for handle, role in p["members"].items():
                ProjectMembership.objects.create(project=project, user=users[handle], role=role)
            projects[p["key"]] = project

        sprints = {}
        for key, name, start, end, status, committed, completed in SPRINTS:
            if key not in projects:
                continue
            sprints[name] = Sprint.objects.create(
                project=projects[key], name=name, status=status,
                start_date=today + timedelta(days=start), end_date=today + timedelta(days=end),
                completed_at=now + timedelta(days=end) if status == "completed" else None,
                committed_points=committed if status == "completed" else None, completed_points=completed,
            )

        by_project_number = {}
        for index, (key, issue_type, title, status, priority, assignee, reporter, extra) in enumerate(ISSUES):
            project = projects.get(key)
            if project is None:
                continue
            project.issue_counter += 1
            number = project.issue_counter
            parent = by_project_number.get((key, extra.get("parent")))
            issue = Issue.objects.create(
                project=project, number=number, key=f"{key}-{number}", issue_type=issue_type, title=title,
                status=status, priority=priority, assignee=users.get(assignee), reporter=users[reporter],
                labels=extra.get("labels", []), story_points=extra.get("points"), parent=parent,
                due_date=today + timedelta(days=extra["due"]) if "due" in extra else None,
                sprint=sprints.get(extra.get("sprint")), rank=(index + 1) * 1000.0,
                completed_at=now - timedelta(days=extra.get("completed", 1)) if status == "done" else None,
            )
            by_project_number[(key, number)] = issue
            activity.record(issue, users[reporter], Activity.Kind.CREATED)
        for project in projects.values():
            project.save(update_fields=["issue_counter"])

        for key, author, body in COMMENTS:
            issue = Issue.objects.filter(project__workspace=ws, key=key).first()
            if issue:
                Comment.objects.create(issue=issue, author=users[author], body=body)
                activity.record(issue, users[author], Activity.Kind.COMMENTED)

        silu = users["silu"]
        for issue in Issue.objects.filter(project__workspace=ws, assignee=silu).exclude(status="done")[:3]:
            Notification.objects.create(recipient=silu, workspace=ws, actor=issue.reporter, category="assignment",
                                        summary="assigned you to", issue=issue, project=issue.project)


def settings_password():
    import os

    return os.environ.get("SEED_PASSWORD")
