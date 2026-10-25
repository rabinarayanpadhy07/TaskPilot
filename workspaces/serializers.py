from django.contrib.auth import get_user_model
from rest_framework import serializers

from workspaces import access
from workspaces.models import (
    Invitation,
    Team,
    Workspace,
    WorkspaceMembership,
    slug_validator,
)
from workspaces.roles import WorkspaceRole

User = get_user_model()


class WorkspaceSerializer(serializers.ModelSerializer):
    """
    `role`, `permissions` and `assignable_roles` describe the CALLER and are
    computed from their membership. They are UI hints; every endpoint enforces
    the same rules itself.
    """

    slug = serializers.CharField(min_length=3, max_length=40, validators=[slug_validator])
    role = serializers.SerializerMethodField()
    permissions = serializers.SerializerMethodField()
    assignable_roles = serializers.SerializerMethodField()
    member_ids = serializers.SerializerMethodField()

    class Meta:
        model = Workspace
        fields = ["id", "name", "slug", "role", "permissions", "assignable_roles", "member_ids", "created_at"]
        read_only_fields = ["id", "created_at"]

    def _membership(self, workspace):
        memberships = self.context.get("memberships_by_workspace")
        if memberships is not None:
            return memberships.get(workspace.pk)
        return self.context.get("membership")

    def get_role(self, workspace) -> str | None:
        membership = self._membership(workspace)
        return membership.role if membership else None

    def get_permissions(self, workspace) -> list[str]:
        return sorted(access.workspace_permissions(self._membership(workspace)))

    def get_assignable_roles(self, workspace) -> list[str]:
        return [str(role) for role in access.roles_assignable_by(self._membership(workspace))]

    def get_member_ids(self, workspace) -> list[int]:
        return [m.user_id for m in workspace.memberships.all()]  # prefetched by the views

    def validate_slug(self, value):
        qs = Workspace.objects.filter(slug=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("This URL is already in use.")
        return value


class MemberSerializer(serializers.ModelSerializer):
    """A workspace member, flattened (the client expects user fields at the top level)."""

    id = serializers.IntegerField(source="user.id", read_only=True)
    email = serializers.EmailField(source="user.email", read_only=True)
    full_name = serializers.CharField(source="user.full_name", read_only=True)
    title = serializers.CharField(source="user.title", read_only=True)
    role = serializers.ChoiceField(choices=WorkspaceRole.choices)
    permissions = serializers.SerializerMethodField()

    class Meta:
        model = WorkspaceMembership
        fields = ["id", "email", "full_name", "title", "role", "joined_at", "permissions"]
        read_only_fields = ["joined_at"]

    def get_permissions(self, target) -> dict[str, bool]:
        actor = self.context["membership"]
        is_self = actor.pk == target.pk
        return {
            "can_change_role": access.can_manage_member(actor, target),
            "can_remove": access.can_manage_member(actor, target),
            "can_leave": is_self,
        }


class InvitationSerializer(serializers.ModelSerializer):
    role = serializers.ChoiceField(choices=WorkspaceRole.choices)
    invited_by = serializers.SerializerMethodField()
    permissions = serializers.SerializerMethodField()

    class Meta:
        model = Invitation
        fields = ["id", "email", "role", "invited_by", "created_at", "expires_at", "permissions"]
        read_only_fields = ["id", "created_at", "expires_at"]

    def get_invited_by(self, invitation) -> dict | None:
        user = invitation.invited_by
        return {"id": user.id, "full_name": user.full_name, "email": user.email} if user else None

    def get_permissions(self, invitation) -> dict[str, bool]:
        actor = self.context["membership"]
        return {"can_revoke": access.can_manage_role(actor.role, invitation.role)}


class PublicInvitationSerializer(serializers.ModelSerializer):
    """What an unauthenticated holder of the link may see. Keep this minimal."""

    workspace = serializers.SerializerMethodField()
    invited_by = serializers.SerializerMethodField()

    class Meta:
        model = Invitation
        fields = ["workspace", "email", "role", "invited_by", "expires_at"]

    def get_workspace(self, invitation) -> dict:
        ws = invitation.workspace
        return {"id": ws.id, "name": ws.name, "slug": ws.slug}

    def get_invited_by(self, invitation) -> dict | None:
        user = invitation.invited_by
        return {"full_name": user.full_name, "email": user.email} if user else None


class TeamSerializer(serializers.ModelSerializer):
    member_ids = serializers.PrimaryKeyRelatedField(source="members", many=True, queryset=User.objects.none(), required=False)

    class Meta:
        model = Team
        fields = ["id", "name", "member_ids"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        membership = self.context.get("membership")
        # Only people in this workspace can be added to its teams
        self.fields["member_ids"].child_relation.queryset = (
            User.objects.filter(workspace_memberships__workspace_id=membership.workspace_id) if membership else User.objects.none()
        )

    def validate_name(self, value):
        membership = self.context["membership"]
        qs = Team.objects.filter(workspace_id=membership.workspace_id, name__iexact=value.strip())
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("A team with this name already exists.")
        return value.strip()
