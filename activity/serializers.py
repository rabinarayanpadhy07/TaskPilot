from rest_framework import serializers

from activity.models import Activity


class ActivitySerializer(serializers.ModelSerializer):
    issue_key = serializers.CharField(source="issue.key", read_only=True)
    issue_title = serializers.CharField(source="issue.title", read_only=True)
    project = serializers.IntegerField(source="issue.project_id", read_only=True)

    class Meta:
        model = Activity
        fields = ["id", "issue", "issue_key", "issue_title", "project", "actor", "kind", "field", "from_value", "to_value", "created_at"]
        read_only_fields = fields
