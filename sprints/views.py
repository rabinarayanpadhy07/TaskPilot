from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import generics, mixins, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from issues import services as issue_services
from issues.models import Issue
from issues.stats import sprint_stats
from sprints.models import Sprint
from sprints.serializers import CompleteSprintSerializer, SprintSerializer
from workspaces import access
from workspaces.permissions import (
    PROJECT_LEVEL,
    IsWorkspaceMember,
    ProjectObjectPermission,
    WorkspaceScopedMixin,
)
from workspaces.roles import ProjectPerm

STATUS_ORDER = {Sprint.Status.ACTIVE: 0, Sprint.Status.PLANNED: 1, Sprint.Status.COMPLETED: 2}


class ProjectSprintListCreateView(WorkspaceScopedMixin, generics.ListCreateAPIView):
    """/workspaces/:ws/projects/:project_pk/sprints/ — GET (project.view): active, planned, completed. POST (sprints.manage)."""

    serializer_class = SprintSerializer
    permission_classes = [IsWorkspaceMember]
    workspace_perms = {"post": PROJECT_LEVEL}

    @property
    def project(self):
        if not hasattr(self, "_project"):
            self._project = get_object_or_404(access.visible_projects(self.membership), pk=self.kwargs["project_pk"])
        return self._project

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        perm = ProjectPerm.VIEW if request.method in ("GET", "HEAD", "OPTIONS") else ProjectPerm.SPRINTS_MANAGE
        access.assert_project_perm(self.membership, self.project, perm, "Only project managers can plan sprints.")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Sprint.objects.none()
        return Sprint.objects.filter(project=self.project).order_by("start_date", "id")

    def list(self, request, *args, **kwargs):
        sprints = sorted(self.get_queryset(), key=lambda s: (STATUS_ORDER[s.status], str(s.start_date or ""), s.id))
        page = self.paginate_queryset(sprints)
        return self.get_paginated_response(self.get_serializer(page, many=True).data)

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if self.membership is not None and "project_pk" in self.kwargs:
            context["project"] = self.project
        return context

    def perform_create(self, serializer):
        serializer.save(project=self.project)


class SprintViewSet(WorkspaceScopedMixin, mixins.RetrieveModelMixin, mixins.UpdateModelMixin, viewsets.GenericViewSet):
    """/workspaces/:ws/sprints/:id/ — GET, PATCH; POST .../start/ and .../complete/ (sprints.manage)."""

    lookup_value_converter = "int"

    serializer_class = SprintSerializer
    permission_classes = [IsWorkspaceMember, ProjectObjectPermission]
    http_method_names = ["get", "post", "patch", "head", "options"]
    workspace_perms = {"partial_update": PROJECT_LEVEL, "start": PROJECT_LEVEL, "complete": PROJECT_LEVEL}
    project_perms = {
        "partial_update": ProjectPerm.SPRINTS_MANAGE,
        "start": ProjectPerm.SPRINTS_MANAGE,
        "complete": ProjectPerm.SPRINTS_MANAGE,
    }

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Sprint.objects.none()
        return Sprint.objects.filter(project__in=access.visible_projects(self.membership)).select_related("project")

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if "pk" in self.kwargs and self.membership is not None:
            sprint = self.get_queryset().filter(pk=self.kwargs["pk"]).first()
            context["project"] = sprint.project if sprint else None
        return context

    def perform_update(self, serializer):
        if serializer.instance.status == Sprint.Status.COMPLETED:
            raise ValidationError({"detail": "Completed sprints can't be edited."})
        serializer.save()

    @extend_schema(request=SprintSerializer, responses=SprintSerializer)
    @action(detail=True, methods=["post"])
    def start(self, request, *args, **kwargs):
        sprint = self.get_object()
        serializer = self.get_serializer(sprint, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            with transaction.atomic():
                sprint = Sprint.objects.select_for_update().get(pk=sprint.pk)
                if sprint.status != Sprint.Status.PLANNED:
                    raise ValidationError({"detail": "Only planned sprints can be started."})
                active = Sprint.objects.filter(project_id=sprint.project_id, status=Sprint.Status.ACTIVE).first()
                if active:
                    raise ValidationError({"detail": f"{active.name} is still active. Complete it before starting another sprint."})
                start, end = data.get("start_date") or sprint.start_date, data.get("end_date") or sprint.end_date
                if not start or not end:
                    raise ValidationError({"start_date": ["Start and end dates are required."]})
                stats = sprint_stats(sprint)
                if stats["total"] == 0:
                    raise ValidationError({"detail": "Add at least one issue before starting the sprint."})
                sprint.start_date, sprint.end_date = start, end
                if "goal" in data:
                    sprint.goal = data["goal"]
                sprint.status = Sprint.Status.ACTIVE
                sprint.committed_points = stats["points"]
                sprint.save()
        except IntegrityError:
            # The partial unique index caught a concurrent start
            raise ValidationError({"detail": "Another sprint in this project was just started."}) from None
        return Response(self.get_serializer(sprint).data)

    @extend_schema(request=CompleteSprintSerializer, responses=SprintSerializer)
    @action(detail=True, methods=["post"])
    def complete(self, request, *args, **kwargs):
        sprint = self.get_object()
        payload = CompleteSprintSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        target_id = payload.validated_data.get("move_open_issues_to")
        with transaction.atomic():
            sprint = Sprint.objects.select_for_update().get(pk=sprint.pk)
            if sprint.status != Sprint.Status.ACTIVE:
                raise ValidationError({"detail": "Only the active sprint can be completed."})
            target = None
            if target_id is not None:
                target = Sprint.objects.filter(pk=target_id, project_id=sprint.project_id, status=Sprint.Status.PLANNED).first()
                if target is None:
                    raise ValidationError({"move_open_issues_to": ["Choose a planned sprint in this project."]})
            sprint.completed_points = sprint_stats(sprint)["done_points"]
            open_issues = Issue.objects.select_for_update().select_related("project").filter(sprint=sprint).exclude(status=Issue.Status.DONE)
            for issue in open_issues:
                issue_services.update_issue(request.user, issue, {"sprint": target})
            sprint.status = Sprint.Status.COMPLETED
            sprint.completed_at = timezone.now()
            sprint.save()
        return Response(self.get_serializer(sprint).data)
