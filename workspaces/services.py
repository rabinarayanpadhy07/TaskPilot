"""
Membership and invitation business rules. Views call these after the request
has passed IsWorkspaceMember; the functions re-check the rule-specific parts
(role hierarchy, last owner, self-service) so they are safe to reuse.
"""

from django.conf import settings
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from workspaces import access
from workspaces.models import (
    Invitation,
    Team,
    Workspace,
    WorkspaceMembership,
    hash_token,
)
from workspaces.roles import WorkspacePerm, WorkspaceRole


def create_workspace(user, *, name, slug):
    with transaction.atomic():
        workspace = Workspace.objects.create(name=name, slug=slug, created_by=user)
        WorkspaceMembership.objects.create(workspace=workspace, user=user, role=WorkspaceRole.OWNER)
    return workspace


def _owner_count(workspace_id):
    return WorkspaceMembership.objects.filter(workspace_id=workspace_id, role=WorkspaceRole.OWNER).count()


def change_member_role(actor, target, new_role):
    if new_role not in WorkspaceRole.values:
        raise ValidationError({"role": [f'"{new_role}" is not a valid role.']})
    if actor.pk == target.pk:
        raise PermissionDenied("You can't change your own role. Ask another owner or admin.")
    if not access.can_manage_member(actor, target):
        raise PermissionDenied("You don't have permission to change this member's role.")
    if new_role not in access.roles_assignable_by(actor):
        raise PermissionDenied(f"You can't assign the {WorkspaceRole(new_role).label} role.")

    with transaction.atomic():
        target = WorkspaceMembership.objects.select_for_update().get(pk=target.pk)
        if target.role == WorkspaceRole.OWNER and new_role != WorkspaceRole.OWNER and _owner_count(target.workspace_id) <= 1:
            raise ValidationError({"role": ["A workspace needs at least one owner."]})
        target.role = new_role
        target.save(update_fields=["role"])
    return target


def remove_member(actor, target):
    """Remove `target`, or let `actor` leave when target is themselves."""
    leaving = actor.pk == target.pk
    if not leaving and not access.can_manage_member(actor, target):
        raise PermissionDenied("You don't have permission to remove this member.")

    from issues.models import Issue
    from projects.models import Project, ProjectMembership

    with transaction.atomic():
        target = WorkspaceMembership.objects.select_for_update().get(pk=target.pk)
        if target.role == WorkspaceRole.OWNER and _owner_count(target.workspace_id) <= 1:
            raise ValidationError({"detail": "The last owner can't leave or be removed. Make someone else an owner first."})
        ws_id, user_id = target.workspace_id, target.user_id
        # Their access to this workspace's content ends with the membership
        ProjectMembership.objects.filter(project__workspace_id=ws_id, user_id=user_id).delete()
        Team.members.through.objects.filter(team__workspace_id=ws_id, user_id=user_id).delete()
        Project.objects.filter(workspace_id=ws_id, lead_id=user_id).update(lead=None)
        Issue.objects.filter(project__workspace_id=ws_id, assignee_id=user_id).exclude(status=Issue.Status.DONE).update(assignee=None)
        Invitation.objects.pending().filter(workspace_id=ws_id, email__iexact=target.user.email).update(revoked_at=timezone.now())
        target.delete()


def invite_url(raw_token):
    return f"{settings.FRONTEND_URL.rstrip('/')}/invite/{raw_token}"


def create_invitation(actor, *, email, role):
    """Returns (invitation, raw_token). Re-inviting an address replaces its open invitation."""
    email = email.strip().lower()
    if not access.has_workspace_perm(actor, WorkspacePerm.MEMBERS_INVITE):
        raise PermissionDenied("You don't have permission to invite people.")
    if role not in access.roles_assignable_by(actor):
        raise PermissionDenied(f"You can't invite people as {WorkspaceRole(role).label}.")
    if WorkspaceMembership.objects.filter(workspace_id=actor.workspace_id, user__email__iexact=email).exists():
        raise ValidationError({"email": ["This person is already a member of the workspace."]})

    raw, token_hash = Invitation.new_token()
    try:
        with transaction.atomic():
            Invitation.objects.filter(
                workspace_id=actor.workspace_id, email__iexact=email, accepted_at__isnull=True, revoked_at__isnull=True
            ).update(revoked_at=timezone.now())
            invitation = Invitation.objects.create(
                workspace_id=actor.workspace_id,
                email=email,
                role=role,
                token_hash=token_hash,
                invited_by=actor.user,
                expires_at=timezone.now() + settings.INVITATION_TTL,
            )
    except IntegrityError:
        raise ValidationError({"email": ["An invitation for this address was just created. Try again."]}) from None

    send_mail(
        subject=f"You're invited to {actor.workspace.name} on TaskPilot",
        message=(
            f"{actor.user.full_name or actor.user.email} invited you to join {actor.workspace.name} "
            f"as {WorkspaceRole(role).label}.\n\nAccept the invitation: {invite_url(raw)}\n"
        ),
        from_email=None,
        recipient_list=[email],
        fail_silently=True,
    )
    return invitation, raw


def get_pending_invitation(raw_token):
    invitation = (
        Invitation.objects.pending().select_related("workspace", "invited_by").filter(token_hash=hash_token(raw_token or "")).first()
    )
    if invitation is None:
        raise NotFound("This invitation is invalid or has expired.")
    return invitation


def accept_invitation(user, raw_token):
    with transaction.atomic():
        invitation = get_pending_invitation(raw_token)
        invitation = Invitation.objects.select_for_update().get(pk=invitation.pk)
        if not invitation.is_pending:
            raise NotFound("This invitation is invalid or has expired.")
        # The token alone is not enough: it must be redeemed by the invited address
        if invitation.email.lower() != user.email.lower():
            raise PermissionDenied("This invitation was sent to a different email address.")
        WorkspaceMembership.objects.get_or_create(
            workspace_id=invitation.workspace_id, user=user, defaults={"role": invitation.role}
        )
        invitation.accepted_at = timezone.now()
        invitation.accepted_by = user
        invitation.save(update_fields=["accepted_at", "accepted_by"])
    return invitation.workspace


def revoke_invitation(actor, invitation):
    if invitation.workspace_id != actor.workspace_id:
        raise NotFound()
    if not access.has_workspace_perm(actor, WorkspacePerm.MEMBERS_INVITE):
        raise PermissionDenied()
    if not access.can_manage_role(actor.role, invitation.role):
        raise PermissionDenied(f"You can't revoke an invitation for the {WorkspaceRole(invitation.role).label} role.")
    invitation.revoked_at = timezone.now()
    invitation.save(update_fields=["revoked_at"])
