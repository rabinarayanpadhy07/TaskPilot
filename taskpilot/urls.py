from django.conf import settings
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework.routers import SimpleRouter

from activity.views import IssueActivityView, WorkspaceActivityView
from issues.views import (
    AttachmentDownloadView,
    AttachmentViewSet,
    CommentViewSet,
    IssueViewSet,
    LabelListView,
    SearchView,
)
from notifications.views import MarkReadView, MarkUnreadView, NotificationListView
from projects.views import ProjectMemberViewSet, ProjectViewSet
from reports.views import WorkspaceReportView
from sprints.views import ProjectSprintListCreateView, SprintViewSet
from workspaces.views import (
    AcceptInvitationView,
    InvitationViewSet,
    MemberViewSet,
    PublicInvitationView,
    TeamViewSet,
    WorkspaceDetailView,
    WorkspaceListCreateView,
)

# Everything below /workspaces/<workspace_pk>/ is authorized against the
# caller's membership in that workspace (workspaces/permissions.py).
workspace_router = SimpleRouter()
workspace_router.register("members", MemberViewSet, basename="workspace-member")
workspace_router.register("invitations", InvitationViewSet, basename="workspace-invitation")
workspace_router.register("teams", TeamViewSet, basename="team")
workspace_router.register("projects", ProjectViewSet, basename="project")
workspace_router.register("issues", IssueViewSet, basename="issue")
workspace_router.register("sprints", SprintViewSet, basename="sprint")

project_router = SimpleRouter()
project_router.register("members", ProjectMemberViewSet, basename="project-member")

issue_router = SimpleRouter()
issue_router.register("comments", CommentViewSet, basename="issue-comment")
issue_router.register("attachments", AttachmentViewSet, basename="issue-attachment")

ws = "workspaces/<int:workspace_pk>/"
issue_prefix = ws + "issues/<str:issue_key>/"

api_patterns = [
    path("auth/", include("accounts.urls")),
    path("workspaces/", WorkspaceListCreateView.as_view(), name="workspace-list"),
    path("workspaces/<int:pk>/", WorkspaceDetailView.as_view(), name="workspace-detail"),
    path(ws + "reports/", WorkspaceReportView.as_view(), name="workspace-reports"),
    path(ws + "labels/", LabelListView.as_view(), name="workspace-labels"),
    path(ws + "search/", SearchView.as_view(), name="workspace-search"),
    path(ws + "activity/", WorkspaceActivityView.as_view(), name="workspace-activity"),
    path(ws + "projects/<int:project_pk>/sprints/", ProjectSprintListCreateView.as_view(), name="project-sprints"),
    path(ws + "projects/<int:project_pk>/", include(project_router.urls)),
    path(issue_prefix + "activity/", IssueActivityView.as_view(), name="issue-activity"),
    path(issue_prefix, include(issue_router.urls)),
    path(ws, include(workspace_router.urls)),
    path("notifications/", NotificationListView.as_view(), name="notification-list"),
    path("notifications/mark-read/", MarkReadView.as_view(), name="notification-mark-read"),
    path("notifications/<int:pk>/mark-unread/", MarkUnreadView.as_view(), name="notification-mark-unread"),
    path("invitations/<str:token>/", PublicInvitationView.as_view(), name="invitation-detail"),
    path("invitations/<str:token>/accept/", AcceptInvitationView.as_view(), name="invitation-accept"),
    path("attachments/<str:token>/", AttachmentDownloadView.as_view(), name="attachment-download"),
]

if settings.API_DOCS_ENABLED:
    api_patterns += [
        path("schema/", SpectacularAPIView.as_view(), name="api-schema"),
        path("docs/", SpectacularSwaggerView.as_view(url_name="api-schema"), name="api-docs"),
    ]

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include(api_patterns)),
]
