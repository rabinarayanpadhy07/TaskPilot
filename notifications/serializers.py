from rest_framework import serializers

from notifications.models import Notification


class NotificationSerializer(serializers.ModelSerializer):
    issue_key = serializers.CharField(source="issue.key", read_only=True, default=None)
    issue_title = serializers.CharField(source="issue.title", read_only=True, default=None)

    class Meta:
        model = Notification
        fields = ["id", "category", "actor", "summary", "issue_key", "issue_title", "project", "excerpt", "created_at", "read"]
        read_only_fields = fields


class MarkReadSerializer(serializers.Serializer):
    workspace = serializers.IntegerField()
    ids = serializers.ListField(child=serializers.IntegerField(), allow_null=True, required=False, max_length=500)
