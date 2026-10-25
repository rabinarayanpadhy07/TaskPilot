from datetime import date, timedelta

from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from reports.services import MAX_RANGE_DAYS, build_report
from workspaces import access
from workspaces.permissions import IsWorkspaceMember, WorkspaceScopedMixin
from workspaces.roles import WorkspacePerm


def _parse_date(value, field):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValidationError({field: ["Use the YYYY-MM-DD format."]}) from None


class WorkspaceReportView(WorkspaceScopedMixin, APIView):
    """
    GET /workspaces/:ws/reports/?project=<id>&from=YYYY-MM-DD&to=YYYY-MM-DD
    Defaults to the last 30 days across every project the caller can see.
    """

    permission_classes = [IsWorkspaceMember]
    workspace_perms = {"get": WorkspacePerm.REPORTS_VIEW}

    @extend_schema(
        parameters=[OpenApiParameter("project", int), OpenApiParameter("from", OpenApiTypes.DATE), OpenApiParameter("to", OpenApiTypes.DATE)],
        responses=OpenApiTypes.OBJECT,
    )
    def get(self, request, workspace_pk):
        params = request.query_params
        today = timezone.localdate()
        date_to = _parse_date(params["to"], "to") if params.get("to") else today
        date_from = _parse_date(params["from"], "from") if params.get("from") else date_to - timedelta(days=29)
        if date_from > date_to:
            raise ValidationError({"from": ["The start date must be on or before the end date."]})
        if (date_to - date_from).days >= MAX_RANGE_DAYS:
            raise ValidationError({"from": [f"Choose a range of at most {MAX_RANGE_DAYS} days."]})

        projects = access.visible_projects(self.membership)
        if params.get("project"):
            project_id = params["project"]
            if not project_id.isdigit():
                raise ValidationError({"project": ["Invalid project."]})
            projects = projects.filter(pk=get_object_or_404(projects, pk=project_id).pk)

        return Response(build_report(projects, date_from, date_to))
