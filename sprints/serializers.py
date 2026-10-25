from rest_framework import serializers

from issues.stats import sprint_stats
from sprints.models import Sprint


class SprintSerializer(serializers.ModelSerializer):
    project = serializers.PrimaryKeyRelatedField(read_only=True)
    name = serializers.CharField(max_length=80)
    stats = serializers.SerializerMethodField()

    class Meta:
        model = Sprint
        fields = ["id", "project", "name", "goal", "start_date", "end_date", "status", "completed_at", "committed_points", "completed_points", "stats"]
        read_only_fields = ["id", "status", "completed_at", "committed_points", "completed_points"]

    def get_stats(self, sprint) -> dict:
        return sprint_stats(sprint)

    def validate(self, attrs):
        instance = self.instance if isinstance(self.instance, Sprint) else None
        start = attrs.get("start_date", instance.start_date if instance else None)
        end = attrs.get("end_date", instance.end_date if instance else None)
        if bool(start) != bool(end):
            raise serializers.ValidationError({"end_date": ["Set both a start and an end date, or neither."]})
        if instance and instance.status == Sprint.Status.ACTIVE and not (start and end):
            raise serializers.ValidationError({"end_date": ["An active sprint needs start and end dates."]})
        if start and end:
            if end <= start:
                raise serializers.ValidationError({"end_date": ["The end date must be after the start date."]})
            project = self.context["project"]
            clash = (
                Sprint.objects.filter(project=project, start_date__lte=end, end_date__gte=start)
                .exclude(status=Sprint.Status.COMPLETED)
                .exclude(pk=getattr(instance, "pk", None))
                .first()
            )
            if clash:
                raise serializers.ValidationError({"start_date": [f"These dates overlap {clash.name} ({clash.start_date} – {clash.end_date})."]})
        return attrs


class CompleteSprintSerializer(serializers.Serializer):
    move_open_issues_to = serializers.IntegerField(allow_null=True, required=False)
