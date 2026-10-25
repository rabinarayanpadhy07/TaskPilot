from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import generics

from activity.models import Activity
from activity.serializers import ActivitySerializer
from issues.models import Issue
from workspaces import access
from workspaces.permissions import IsWorkspaceMember, WorkspaceScopedMixin


class IssueActivityView(WorkspaceScopedMixin, generics.ListAPIView):
    """GET /workspaces/:ws/issues/:key/activity/ — oldest first. Read-only: history is written by the server."""

    serializer_class = ActivitySerializer
    permission_classes = [IsWorkspaceMember]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Activity.objects.none()
        issue = get_object_or_404(Issue.objects.filter(project__in=access.visible_projects(self.membership)), key=self.kwargs["issue_key"])
        return Activity.objects.filter(issue=issue).select_related("issue").order_by("created_at", "id")


class WorkspaceActivityView(WorkspaceScopedMixin, generics.ListAPIView):
    """GET /workspaces/:ws/activity/?page_size=20 — newest first, only for projects the caller can see."""

    serializer_class = ActivitySerializer
    permission_classes = [IsWorkspaceMember]

    @extend_schema(parameters=[OpenApiParameter("page_size", int)])
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Activity.objects.none()
        return (
            Activity.objects.filter(workspace_id=self.membership.workspace_id, issue__project__in=access.visible_projects(self.membership))
            .select_related("issue")
            .order_by("-created_at", "-id")
        )
