from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import generics, mixins, permissions, serializers, status, viewsets
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from workspaces import services
from workspaces.models import Invitation, Team, Workspace, WorkspaceMembership
from workspaces.permissions import IsWorkspaceMember, WorkspaceScopedMixin
from workspaces.roles import WorkspacePerm
from workspaces.serializers import (
    InvitationSerializer,
    MemberSerializer,
    PublicInvitationSerializer,
    TeamSerializer,
    WorkspaceSerializer,
)


class WorkspaceListCreateView(generics.ListCreateAPIView):
    """GET: workspaces the caller belongs to (never others). POST: create one; the caller becomes Owner."""

    serializer_class = WorkspaceSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Workspace.objects.none()
        return Workspace.objects.filter(memberships__user=self.request.user).prefetch_related("memberships").order_by("name", "id")

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["memberships_by_workspace"] = {
            m.workspace_id: m for m in WorkspaceMembership.objects.filter(user=self.request.user)
        }
        return context

    def perform_create(self, serializer):
        workspace = services.create_workspace(self.request.user, **serializer.validated_data)
        serializer.instance = workspace
        serializer.context["memberships_by_workspace"] = {
            workspace.pk: workspace.memberships.get(user=self.request.user)
        }


class WorkspaceDetailView(WorkspaceScopedMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = WorkspaceSerializer
    permission_classes = [IsWorkspaceMember]
    workspace_url_kwarg = "pk"
    workspace_perms = {
        "put": WorkspacePerm.UPDATE,
        "patch": WorkspacePerm.UPDATE,
        "delete": WorkspacePerm.DELETE,
    }

    def get_object(self):
        return Workspace.objects.prefetch_related("memberships").get(pk=self.workspace.pk)


class MemberViewSet(WorkspaceScopedMixin, mixins.ListModelMixin, mixins.UpdateModelMixin, mixins.DestroyModelMixin, viewsets.GenericViewSet):
    """
    /workspaces/:ws/members/            GET   everyone in the workspace
    /workspaces/:ws/members/:user_id/   PATCH {role}  (members.manage + hierarchy)
                                        DELETE        remove (members.manage) or leave (self)
    """

    lookup_value_converter = "int"

    serializer_class = MemberSerializer
    permission_classes = [IsWorkspaceMember]
    lookup_field = "user_id"
    workspace_perms = {
        "list": WorkspacePerm.MEMBERS_VIEW,
        "update": WorkspacePerm.MEMBERS_MANAGE,
        "partial_update": WorkspacePerm.MEMBERS_MANAGE,
        # Anyone may leave; removing someone else is checked in the service
        "destroy": WorkspacePerm.VIEW,
    }

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return WorkspaceMembership.objects.none()
        return WorkspaceMembership.objects.filter(workspace=self.workspace).select_related("user").order_by("user__full_name", "user__email")

    def update(self, request, *args, **kwargs):
        target = self.get_object()
        serializer = self.get_serializer(target, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        if "role" not in serializer.validated_data:
            return Response({"role": ["This field is required."]}, status=status.HTTP_400_BAD_REQUEST)
        target = services.change_member_role(self.membership, target, serializer.validated_data["role"])
        return Response(self.get_serializer(target).data)

    def perform_destroy(self, target):
        services.remove_member(self.membership, target)


class InvitationViewSet(WorkspaceScopedMixin, mixins.ListModelMixin, mixins.CreateModelMixin, mixins.DestroyModelMixin, viewsets.GenericViewSet):
    """Pending invitations. DELETE revokes. Requires members.invite."""

    lookup_value_converter = "int"

    serializer_class = InvitationSerializer
    permission_classes = [IsWorkspaceMember]
    workspace_perms = {
        "list": WorkspacePerm.MEMBERS_INVITE,
        "create": WorkspacePerm.MEMBERS_INVITE,
        "destroy": WorkspacePerm.MEMBERS_INVITE,
    }

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Invitation.objects.none()
        return Invitation.objects.pending().filter(workspace=self.workspace).select_related("invited_by")

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        invitation, raw_token = services.create_invitation(self.membership, **serializer.validated_data)
        data = self.get_serializer(invitation).data
        # The raw link is returned once, to the inviter, so it can be shared by hand
        data["invite_url"] = services.invite_url(raw_token)
        return Response(data, status=status.HTTP_201_CREATED)

    def perform_destroy(self, invitation):
        services.revoke_invitation(self.membership, invitation)


class TeamViewSet(WorkspaceScopedMixin, viewsets.ModelViewSet):
    """/workspaces/:ws/teams/ — everyone can browse; owners/admins manage."""

    lookup_value_converter = "int"

    serializer_class = TeamSerializer
    permission_classes = [IsWorkspaceMember]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    workspace_perms = {
        "list": WorkspacePerm.MEMBERS_VIEW,
        "retrieve": WorkspacePerm.MEMBERS_VIEW,
        "create": WorkspacePerm.MEMBERS_MANAGE,
        "partial_update": WorkspacePerm.MEMBERS_MANAGE,
        "destroy": WorkspacePerm.MEMBERS_MANAGE,
    }

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Team.objects.none()
        return Team.objects.filter(workspace=self.workspace).prefetch_related("members")

    def perform_create(self, serializer):
        serializer.save(workspace=self.workspace)


class PublicInvitationView(APIView):
    """GET /invitations/:token/ — public, minimal details for the accept page."""

    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "invitation"

    @extend_schema(responses=PublicInvitationSerializer)
    def get(self, request, token):
        return Response(PublicInvitationSerializer(services.get_pending_invitation(token)).data)


class AcceptInvitationView(APIView):
    """POST /invitations/:token/accept/ — the signed-in user must be the invited address."""

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "invitation"

    @extend_schema(request=None, responses=inline_serializer("AcceptedInvitation", {"workspace": serializers.DictField()}))
    def post(self, request, token):
        workspace = services.accept_invitation(request.user, token)
        return Response({"workspace": {"id": workspace.id, "name": workspace.name, "slug": workspace.slug}})
