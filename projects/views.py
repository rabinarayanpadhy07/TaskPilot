from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import viewsets

from issues.stats import issue_counts_by_project
from projects.models import Project, ProjectMembership
from projects.serializers import ProjectMemberSerializer, ProjectSerializer
from workspaces import access
from workspaces.models import WorkspaceMembership
from workspaces.permissions import (
    PROJECT_LEVEL,
    IsWorkspaceMember,
    ProjectObjectPermission,
    WorkspaceScopedMixin,
)
from workspaces.roles import ProjectPerm, ProjectRole, WorkspacePerm


class ProjectViewSet(WorkspaceScopedMixin, viewsets.ModelViewSet):
    """
    /workspaces/:ws/projects/       GET (visible projects only), POST (projects.create)
    /workspaces/:ws/projects/:id/   GET, PATCH (project.update), DELETE (projects.delete)
    """

    lookup_value_converter = "int"

    serializer_class = ProjectSerializer
    permission_classes = [IsWorkspaceMember, ProjectObjectPermission]
    workspace_perms = {
        "create": WorkspacePerm.PROJECTS_CREATE,
        "update": PROJECT_LEVEL,
        "partial_update": PROJECT_LEVEL,
        "destroy": WorkspacePerm.PROJECTS_DELETE,
    }
    project_perms = {"update": ProjectPerm.SETTINGS_UPDATE, "partial_update": ProjectPerm.SETTINGS_UPDATE}

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Project.objects.none()
        # Hidden (private, non-member) projects are indistinguishable from missing ones
        return access.visible_projects(self.membership).select_related("lead").prefetch_related("memberships")

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if self.membership is not None:
            if self.action in ("retrieve", "update", "partial_update") and "pk" in self.kwargs:
                try:
                    ids = [int(self.kwargs["pk"])]
                except (ValueError, TypeError):
                    ids = list(access.visible_projects(self.membership).values_list("pk", flat=True))
            else:
                ids = list(access.visible_projects(self.membership).values_list("pk", flat=True))
            context["issue_counts"] = issue_counts_by_project(ids)
        return context

    def perform_create(self, serializer):
        user = self.request.user
        data = serializer.validated_data
        status = data.get("status")
        phase = data.get("phase")
        if status == Project.Status.COMPLETED and "phase" not in data:
            serializer.validated_data["phase"] = Project.Phase.COMPLETED
        elif phase == Project.Phase.COMPLETED and "status" not in data:
            serializer.validated_data["status"] = Project.Status.COMPLETED

        with transaction.atomic():
            project = serializer.save(workspace=self.workspace, lead=serializer.validated_data.get("lead") or user)
            ProjectMembership.objects.create(project=project, user=user, role=ProjectRole.MANAGER)
            if project.lead_id and project.lead_id != user.id:
                ProjectMembership.objects.get_or_create(project=project, user_id=project.lead_id, defaults={"role": ProjectRole.MANAGER})

    def perform_update(self, serializer):
        data = serializer.validated_data
        status = data.get("status")
        phase = data.get("phase")

        if status == Project.Status.COMPLETED and "phase" not in data:
            serializer.validated_data["phase"] = Project.Phase.COMPLETED
        elif phase == Project.Phase.COMPLETED and "status" not in data:
            serializer.validated_data["status"] = Project.Status.COMPLETED
        elif phase and phase != Project.Phase.COMPLETED and serializer.instance.status == Project.Status.COMPLETED and "status" not in data:
            serializer.validated_data["status"] = Project.Status.ON_TRACK

        project = serializer.save()
        if project.lead_id:
            ProjectMembership.objects.get_or_create(project=project, user_id=project.lead_id, defaults={"role": ProjectRole.MANAGER})


class ProjectMemberViewSet(WorkspaceScopedMixin, viewsets.ModelViewSet):
    """
    /workspaces/:ws/projects/:project_pk/members/[:user_id/]
    Viewing needs project access; changes need project.members.manage.
    """

    lookup_value_converter = "int"

    serializer_class = ProjectMemberSerializer
    permission_classes = [IsWorkspaceMember]
    lookup_field = "user_id"
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    workspace_perms = {"create": PROJECT_LEVEL, "partial_update": PROJECT_LEVEL, "destroy": PROJECT_LEVEL}

    @property
    def project(self):
        if not hasattr(self, "_project"):
            self._project = get_object_or_404(access.visible_projects(self.membership), pk=self.kwargs["project_pk"])
        return self._project

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        perm = ProjectPerm.VIEW if request.method in ("GET", "HEAD", "OPTIONS") else ProjectPerm.MEMBERS_MANAGE
        access.assert_project_perm(self.membership, self.project, perm, "You don't have permission to manage this project's members.")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return ProjectMembership.objects.none()
        return ProjectMembership.objects.filter(project=self.project).select_related("user", "project").order_by("user__full_name")

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if self.membership is not None and "project_pk" in self.kwargs:
            context["project"] = self.project
            context["workspace_roles"] = {
                m.user_id: m for m in WorkspaceMembership.objects.filter(workspace_id=self.membership.workspace_id)
            }
        return context

    def perform_create(self, serializer):
        user_id = serializer.validated_data["user"]
        target_ws = WorkspaceMembership.objects.filter(workspace_id=self.membership.workspace_id, user_id=user_id).first()
        access.assert_can_assign_project_role(self.membership, serializer.validated_data["role"], target_ws.role)
        serializer.save(project=self.project, user_id=user_id)

    def perform_update(self, serializer):
        target_ws = WorkspaceMembership.objects.filter(workspace_id=self.membership.workspace_id, user_id=serializer.instance.user_id).first()
        access.assert_can_assign_project_role(self.membership, serializer.validated_data["role"], target_ws.role)
        serializer.save()

    def perform_destroy(self, instance):
        if instance.user_id == self.project.lead_id:
            from rest_framework.exceptions import ValidationError
            raise ValidationError("The project lead cannot be removed from the project.")
        instance.delete()
