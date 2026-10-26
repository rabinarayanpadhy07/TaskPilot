from django.conf import settings
from django.core import signing
from django.db import transaction
from django.db.models import Count, F, Func, Q
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import mixins, permissions, serializers, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from activity import services as activity
from activity.models import Activity
from issues import services
from issues.filters import IssueFilter
from issues.models import Attachment, Comment, Issue
from issues.serializers import (
    ATTACHMENT_SIGNER_SALT,
    AttachmentSerializer,
    BulkUpdateSerializer,
    CommentSerializer,
    IssueSerializer,
    MoveSerializer,
    comment_permissions,
)
from notifications import services as notify
from projects.serializers import ProjectSerializer
from workspaces import access
from workspaces.models import WorkspaceMembership
from workspaces.permissions import (
    PROJECT_LEVEL,
    IsWorkspaceMember,
    ProjectObjectPermission,
    WorkspaceScopedMixin,
)
from workspaces.roles import ProjectPerm
from workspaces.serializers import MemberSerializer


def visible_issues(membership):
    return Issue.objects.filter(project__in=access.visible_projects(membership))


class IssueContextMixin:
    """Adds per-project permissions once per request, so lists don't query per issue."""

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if self.membership is not None:
            project_id = self.request.query_params.get("project") if hasattr(self, "request") and self.request else None
            if project_id:
                try:
                    projects = list(access.visible_projects(self.membership).filter(pk=int(project_id)))
                except (ValueError, TypeError):
                    projects = list(access.visible_projects(self.membership))
            else:
                projects = list(access.visible_projects(self.membership))
            context["project_perms"] = access.project_permissions_map(self.membership, projects)
        return context


class IssueViewSet(IssueContextMixin, WorkspaceScopedMixin, viewsets.ModelViewSet):
    """
    /workspaces/:ws/issues/                 GET (filters: project, assignee, sprint, parent, status, issue_type, priority, label, q), POST
    /workspaces/:ws/issues/:key/            GET, PATCH, DELETE
    /workspaces/:ws/issues/:key/move/       POST  drag-and-drop (status/sprint/parent + position)
    /workspaces/:ws/issues/bulk/            POST  same change to many issues, all or nothing
    Only issues of projects visible to the caller exist for them.
    """

    serializer_class = IssueSerializer
    permission_classes = [IsWorkspaceMember, ProjectObjectPermission]
    filterset_class = IssueFilter
    lookup_field = "key"
    lookup_value_regex = r"[A-Z][A-Z0-9]{1,9}-\d+"
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    workspace_perms = {"create": PROJECT_LEVEL, "partial_update": PROJECT_LEVEL, "destroy": PROJECT_LEVEL, "move": PROJECT_LEVEL, "bulk": PROJECT_LEVEL}
    project_perms = {"partial_update": ProjectPerm.ISSUES_WRITE, "move": ProjectPerm.ISSUES_WRITE, "destroy": ProjectPerm.ISSUES_DELETE}

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Issue.objects.none()
        qs = (
            visible_issues(self.membership)
            .select_related("project")
            .annotate(num_comments=Count("comments", distinct=True), num_attachments=Count("attachments", distinct=True))
        )
        return qs.order_by("-updated_at", "-id") if self.action == "list" else qs

    def perform_create(self, serializer):
        project = serializer.validated_data["project"]
        access.assert_project_perm(self.membership, project, ProjectPerm.ISSUES_WRITE, "You can view this project but can't create issues in it.")
        serializer.instance = services.create_issue(self.request.user, serializer.validated_data)

    def perform_update(self, serializer):
        with transaction.atomic():
            issue = Issue.objects.select_for_update().select_related("project").get(pk=serializer.instance.pk)
            serializer.instance = services.update_issue(self.request.user, issue, serializer.validated_data)

    def perform_destroy(self, instance):
        services.delete_issue(instance)

    def _respond(self, issue):
        return Response(self.get_serializer(self.get_queryset().get(pk=issue.pk)).data)

    @extend_schema(request=MoveSerializer, responses=IssueSerializer)
    @action(detail=True, methods=["post"])
    def move(self, request, *args, **kwargs):
        issue = self.get_object()  # object-level check: issues.write
        move = MoveSerializer(data=request.data)
        move.is_valid(raise_exception=True)
        data = move.validated_data
        # Reuse the issue serializer's validation for sprint/status/parent
        fields = {name: data[name] for name in ("status", "sprint", "parent") if name in data}
        checked = self.get_serializer(issue, data=fields, partial=True)
        checked.is_valid(raise_exception=True)
        issue = services.move_issue(request.user, issue, checked.validated_data, data.get("before"), data.get("after"))
        return self._respond(issue)

    @extend_schema(request=BulkUpdateSerializer, responses=IssueSerializer(many=True))
    @action(detail=False, methods=["post"])
    def bulk(self, request, *args, **kwargs):
        payload = BulkUpdateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        keys = list(dict.fromkeys(payload.validated_data["keys"]))
        issues = list(self.get_queryset().filter(key__in=keys))
        if len(issues) != len(keys):
            raise NotFound("One or more issues were not found.")
        changes_by_issue = []
        for issue in issues:
            if ProjectPerm.ISSUES_WRITE not in access.project_permissions(self.membership, issue.project):
                raise PermissionDenied(f"You don't have permission to edit {issue.key}.")
            checked = self.get_serializer(issue, data=payload.validated_data["changes"], partial=True)
            if not checked.is_valid():
                messages = " ".join(str(m) for errors in checked.errors.values() for m in (errors if isinstance(errors, list) else [errors]))
                raise ValidationError({"detail": f"{issue.key}: {messages}"})
            changes_by_issue.append((issue, checked.validated_data))
        with transaction.atomic():
            updated = [services.move_issue(request.user, issue, changes) for issue, changes in changes_by_issue]
        qs = self.get_queryset().filter(pk__in=[i.pk for i in updated])
        return Response(self.get_serializer(qs, many=True).data)


class IssueChildMixin(WorkspaceScopedMixin):
    """Base for /workspaces/:ws/issues/:issue_key/<child>/ endpoints."""

    permission_classes = [IsWorkspaceMember]

    @property
    def issue(self):
        if not hasattr(self, "_issue"):
            self._issue = get_object_or_404(visible_issues(self.membership).select_related("project"), key=self.kwargs["issue_key"])
        return self._issue

    @property
    def issue_perms(self):
        if not hasattr(self, "_issue_perms"):
            self._issue_perms = access.project_permissions(self.membership, self.issue.project)
        return self._issue_perms


class CommentViewSet(IssueChildMixin, viewsets.ModelViewSet):
    """Comments, oldest first. Author edits; author or project manager deletes."""

    lookup_value_converter = "int"

    serializer_class = CommentSerializer
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    workspace_perms = {"create": PROJECT_LEVEL, "partial_update": PROJECT_LEVEL, "destroy": PROJECT_LEVEL}

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Comment.objects.none()
        return Comment.objects.filter(issue=self.issue).select_related("issue__project")

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if self.membership is not None and "issue_key" in self.kwargs:
            context["project_perms"] = {self.issue.project_id: self.issue_perms}
        return context

    def perform_create(self, serializer):
        if ProjectPerm.COMMENT not in self.issue_perms:
            raise PermissionDenied("You don't have permission to comment on this issue.")
        with transaction.atomic():
            comment = serializer.save(issue=self.issue, author=self.request.user)
            activity.record(self.issue, self.request.user, Activity.Kind.COMMENTED)
            notify.comment_added(self.issue, comment, self.request.user)

    def perform_update(self, serializer):
        if not comment_permissions(self.membership, serializer.instance, self.issue_perms)["can_edit"]:
            raise PermissionDenied("Only the author can edit this comment.")
        serializer.save(edited_at=timezone.now())

    def perform_destroy(self, instance):
        if not comment_permissions(self.membership, instance, self.issue_perms)["can_delete"]:
            raise PermissionDenied("Only the author or a project manager can delete this comment.")
        instance.delete()


class AttachmentViewSet(IssueChildMixin, mixins.ListModelMixin, mixins.CreateModelMixin, mixins.DestroyModelMixin, viewsets.GenericViewSet):
    """multipart upload (`file`), 10 MB, allow-listed types. Download links are signed and short-lived."""

    lookup_value_converter = "int"

    serializer_class = AttachmentSerializer
    parser_classes = [MultiPartParser, FormParser]
    workspace_perms = {"create": PROJECT_LEVEL, "destroy": PROJECT_LEVEL}

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Attachment.objects.none()
        return Attachment.objects.filter(issue=self.issue)

    def perform_create(self, serializer):
        if ProjectPerm.ISSUES_WRITE not in self.issue_perms:
            raise PermissionDenied("You don't have permission to add files to this issue.")
        upload = serializer.validated_data["file"]
        with transaction.atomic():
            attachment = serializer.save(
                issue=self.issue,
                uploaded_by=self.request.user,
                file_name=upload.name[-255:],
                size=upload.size,
                content_type=(upload.content_type or "application/octet-stream")[:120],
            )
            activity.record(self.issue, self.request.user, Activity.Kind.ATTACHMENT_ADDED, to_value=attachment.file_name)

    def perform_destroy(self, instance):
        own = instance.uploaded_by_id == self.request.user.id and ProjectPerm.ISSUES_WRITE in self.issue_perms
        if not (own or ProjectPerm.ISSUES_DELETE in self.issue_perms):
            raise PermissionDenied("Only the uploader or a project manager can delete this file.")
        instance.file.delete(save=False)
        instance.delete()


class AttachmentDownloadView(APIView):
    """
    GET /api/attachments/<signed token>/ — the token (from an attachment's `url`)
    binds attachment + user and expires after ATTACHMENT_URL_MAX_AGE. Access is
    re-checked, and files are always served as downloads (never rendered inline).
    """

    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    @extend_schema(responses={(200, "application/octet-stream"): OpenApiTypes.BINARY})
    def get(self, request, token):
        try:
            value = signing.TimestampSigner(salt=ATTACHMENT_SIGNER_SALT).unsign(token, max_age=settings.ATTACHMENT_URL_MAX_AGE)
            attachment_id, user_id = (int(part) for part in value.split(":"))
        except (signing.BadSignature, ValueError):
            raise NotFound() from None
        attachment = get_object_or_404(Attachment.objects.select_related("issue__project"), pk=attachment_id)
        membership = WorkspaceMembership.objects.filter(workspace_id=attachment.issue.project.workspace_id, user_id=user_id, user__is_active=True).first()
        if ProjectPerm.VIEW not in access.project_permissions(membership, attachment.issue.project):
            raise NotFound()
        response = FileResponse(attachment.file.open("rb"), as_attachment=True, filename=attachment.file_name, content_type=attachment.content_type)
        response["X-Content-Type-Options"] = "nosniff"
        response["Cache-Control"] = "private, no-store"
        return response


class LabelListView(WorkspaceScopedMixin, APIView):
    """Distinct labels used in the projects the caller can see."""

    permission_classes = [IsWorkspaceMember]

    @extend_schema(responses=inline_serializer("Labels", {"results": serializers.ListField(child=serializers.CharField())}))
    def get(self, request, workspace_pk):
        labels = (
            visible_issues(self.membership)
            .annotate(label=Func(F("labels"), function="jsonb_array_elements_text"))
            .values_list("label", flat=True)
            .distinct()
            .order_by("label")[:500]
        )
        return Response({"results": list(labels)})


class SearchView(WorkspaceScopedMixin, APIView):
    """Projects, issues and members matching `q` (2+ characters), within what the caller can see."""

    permission_classes = [IsWorkspaceMember]

    @extend_schema(
        parameters=[OpenApiParameter("q", str), OpenApiParameter("limit", int)],
        responses=inline_serializer(
            "SearchResults",
            {"projects": ProjectSerializer(many=True), "issues": IssueSerializer(many=True), "members": MemberSerializer(many=True)},
        ),
    )
    def get(self, request, workspace_pk):
        q = request.query_params.get("q", "").strip()
        try:
            limit = max(1, min(int(request.query_params.get("limit", 8)), 20))
        except ValueError:
            limit = 8
        if len(q) < 2:
            return Response({"projects": [], "issues": [], "members": []})
        context = {"request": request, "membership": self.membership}
        projects = access.visible_projects(self.membership).filter(Q(name__icontains=q) | Q(key__iexact=q)).prefetch_related("memberships")[:limit]
        issues = (
            visible_issues(self.membership)
            .filter(Q(title__icontains=q) | Q(key__iexact=q.upper()))
            .select_related("project")
            .annotate(num_comments=Count("comments", distinct=True), num_attachments=Count("attachments", distinct=True))
            .order_by("-updated_at")[:limit]
        )
        members = (
            WorkspaceMembership.objects.filter(workspace_id=self.membership.workspace_id)
            .filter(Q(user__full_name__icontains=q) | Q(user__email__icontains=q) | Q(user__title__icontains=q))
            .select_related("user")[:limit]
        )
        return Response({
            "projects": ProjectSerializer(projects, many=True, context=context).data,
            "issues": IssueSerializer(issues, many=True, context=context).data,
            "members": MemberSerializer(members, many=True, context=context).data,
        })
