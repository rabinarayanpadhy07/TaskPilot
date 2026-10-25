from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import generics, status
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from notifications.models import Notification
from notifications.serializers import MarkReadSerializer, NotificationSerializer
from workspaces import access


def _membership_or_404(user, workspace_id):
    membership = access.get_membership(user, workspace_id)
    if membership is None:
        raise NotFound()
    return membership


def _visible(user, membership):
    """The caller's notifications in one workspace, minus ones about projects they can no longer see."""
    return Notification.objects.filter(recipient=user, workspace_id=membership.workspace_id).filter(
        project__in=access.visible_projects(membership)
    )


class NotificationListView(generics.ListAPIView):
    """GET /notifications/?workspace=<id> — newest first. Only ever the caller's own notifications."""

    serializer_class = NotificationSerializer

    @extend_schema(parameters=[OpenApiParameter("workspace", int, required=True)])
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Notification.objects.none()
        membership = _membership_or_404(self.request.user, self.request.query_params.get("workspace"))
        return _visible(self.request.user, membership).select_related("issue").order_by("-created_at", "-id")


class MarkReadView(APIView):
    """POST /notifications/mark-read/ { workspace, ids: [..] | null } (null = all)."""

    @extend_schema(request=MarkReadSerializer, responses={204: None})
    def post(self, request):
        payload = MarkReadSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        membership = _membership_or_404(request.user, payload.validated_data["workspace"])
        qs = Notification.objects.filter(recipient=request.user, workspace_id=membership.workspace_id, read=False)
        ids = payload.validated_data.get("ids")
        if ids is not None:
            qs = qs.filter(pk__in=ids)
        qs.update(read=True)
        return Response(status=status.HTTP_204_NO_CONTENT)


class MarkUnreadView(APIView):
    @extend_schema(request=None, responses={204: None})
    def post(self, request, pk):
        notification = get_object_or_404(Notification, pk=pk, recipient=request.user)
        notification.read = False
        notification.save(update_fields=["read"])
        return Response(status=status.HTTP_204_NO_CONTENT)
