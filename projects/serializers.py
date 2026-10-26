from django.contrib.auth import get_user_model
from rest_framework import serializers

from issues.stats import empty_issue_counts
from projects.models import Project, ProjectMembership
from workspaces import access
from workspaces.models import WorkspaceMembership
from workspaces.roles import (
    MAX_PROJECT_ROLE,
    PROJECT_ROLE_RANK,
    ProjectRole,
    WorkspaceRole,
)

User = get_user_model()


class ProjectSerializer(serializers.ModelSerializer):
    """
    `workspace` is read-only and always set from the URL's workspace by the
    view. `my_role` / `permissions` describe the caller (UI hints only).
    """

    workspace = serializers.PrimaryKeyRelatedField(read_only=True)
    lead = serializers.PrimaryKeyRelatedField(read_only=False, allow_null=True, required=False, queryset=User.objects.none())
    members = serializers.SerializerMethodField()
    issue_counts = serializers.SerializerMethodField()
    my_role = serializers.SerializerMethodField()
    permissions = serializers.SerializerMethodField()

    class Meta:
        model = Project
        fields = [
            "id", "workspace", "key", "name", "description", "status", "phase", "target_date", "visibility", "lead",
            "members", "issue_counts", "my_role", "permissions", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        membership = self.context.get("membership")
        # A lead can only be someone in THIS workspace
        self.fields["lead"].queryset = (
            User.objects.filter(workspace_memberships__workspace_id=membership.workspace_id) if membership else User.objects.none()
        )
        if isinstance(self.instance, Project):
            self.fields["key"].read_only = True

    def validate_key(self, value):
        membership = self.context.get("membership")
        if membership and Project.objects.filter(workspace_id=membership.workspace_id, key=value).exists():
            raise serializers.ValidationError(f"A project with key {value} already exists in this workspace.")
        return value

    def get_members(self, project) -> list[int]:
        return [m.user_id for m in project.memberships.all()]

    def get_issue_counts(self, project) -> dict:
        return self.context.get("issue_counts", {}).get(project.pk, empty_issue_counts())

    def _project_membership(self, project):
        membership = self.context.get("membership")
        if not membership:
            return None
        return next((m for m in project.memberships.all() if m.user_id == membership.user_id), None)

    def get_my_role(self, project) -> str | None:
        membership = self.context.get("membership")
        if not membership:
            return None
        return access.effective_project_role(membership, project, self._project_membership(project))

    def get_permissions(self, project) -> list[str]:
        membership = self.context.get("membership")
        if not membership:
            return []
        return sorted(access.project_permissions(membership, project, self._project_membership(project)))


class ProjectMemberSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(source="user.id", read_only=True)
    user = serializers.IntegerField(write_only=True, required=False)
    email = serializers.EmailField(source="user.email", read_only=True)
    full_name = serializers.CharField(source="user.full_name", read_only=True)
    title = serializers.CharField(source="user.title", read_only=True)
    role = serializers.ChoiceField(choices=ProjectRole.choices)
    workspace_role = serializers.SerializerMethodField()
    effective_role = serializers.SerializerMethodField()

    class Meta:
        model = ProjectMembership
        fields = ["id", "user", "email", "full_name", "title", "role", "workspace_role", "effective_role", "added_at"]
        read_only_fields = ["added_at"]

    def _workspace_membership(self, pm):
        return self.context["workspace_roles"].get(pm.user_id)

    def get_workspace_role(self, pm) -> str | None:
        ws = self._workspace_membership(pm)
        return ws.role if ws else None

    def get_effective_role(self, pm) -> str | None:
        ws = self._workspace_membership(pm)
        return access.effective_project_role(ws, pm.project, pm) if ws else None

    def validate(self, attrs):
        project = self.context["project"]
        user_id = attrs.get("user", self.instance.user_id if self.instance else None)
        if user_id is None:
            raise serializers.ValidationError({"user": ["This field is required."]})
        ws = WorkspaceMembership.objects.filter(workspace_id=project.workspace_id, user_id=user_id).first()
        if ws is None:
            # Same message whether the user exists elsewhere or not: no cross-workspace probing
            raise serializers.ValidationError({"user": ["Choose a member of this workspace."]})
        role = attrs.get("role")
        ceiling = MAX_PROJECT_ROLE[WorkspaceRole(ws.role)]
        if role and PROJECT_ROLE_RANK[role] > PROJECT_ROLE_RANK[ceiling]:
            raise serializers.ValidationError(
                {"role": [f"{WorkspaceRole(ws.role).label}s can have at most {ProjectRole(ceiling).label.lower()} access to a project."]}
            )
        if self.instance is None and ProjectMembership.objects.filter(project=project, user_id=user_id).exists():
            raise serializers.ValidationError({"user": ["This person is already a member of the project."]})
        return attrs
