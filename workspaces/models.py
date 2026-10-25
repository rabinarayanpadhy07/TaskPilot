import hashlib
import secrets

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from workspaces.roles import WorkspaceRole

slug_validator = RegexValidator(r"^[a-z0-9]+(-[a-z0-9]+)*$", "Use lowercase letters, numbers and single hyphens.")


class Workspace(models.Model):
    name = models.CharField(max_length=80)
    slug = models.SlugField(max_length=40, unique=True, validators=[slug_validator])
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class WorkspaceMembership(models.Model):
    """
    The ONLY source of a user's workspace role. Every workspace-scoped request
    looks this row up for (request.user, workspace in the URL); without one the
    workspace does not exist as far as that user is concerned (404).
    """

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="workspace_memberships")
    role = models.CharField(max_length=20, choices=WorkspaceRole.choices, default=WorkspaceRole.DEVELOPER)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["workspace", "user"], name="unique_workspace_member"),
            models.CheckConstraint(condition=Q(role__in=WorkspaceRole.values), name="workspace_member_role_valid"),
        ]
        indexes = [models.Index(fields=["user", "workspace"])]

    def __str__(self):
        return f"{self.user} in {self.workspace} ({self.role})"


class Team(models.Model):
    """A named group of workspace members (used for browsing people, not for access)."""

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="teams")
    name = models.CharField(max_length=80)
    members = models.ManyToManyField(settings.AUTH_USER_MODEL, related_name="teams", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["workspace", "name"], name="unique_team_name_per_workspace")]

    def __str__(self):
        return self.name


def hash_token(raw_token):
    return hashlib.sha256(raw_token.encode()).hexdigest()


class InvitationQuerySet(models.QuerySet):
    def pending(self):
        return self.filter(accepted_at__isnull=True, revoked_at__isnull=True, expires_at__gt=timezone.now())


class Invitation(models.Model):
    """
    An invitation to join a workspace with a role. Only a SHA-256 hash of the
    token is stored; the raw token exists in the invitation link alone.
    """

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="invitations")
    email = models.EmailField()
    role = models.CharField(max_length=20, choices=WorkspaceRole.choices)
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    invited_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    accepted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    revoked_at = models.DateTimeField(null=True, blank=True)

    objects = InvitationQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["workspace", "email"])]
        constraints = [
            models.CheckConstraint(condition=Q(role__in=WorkspaceRole.values), name="invitation_role_valid"),
            # One open invitation per address per workspace (re-inviting replaces it)
            models.UniqueConstraint(
                fields=["workspace", "email"],
                condition=Q(accepted_at__isnull=True, revoked_at__isnull=True),
                name="unique_open_invitation",
            )
        ]

    @staticmethod
    def new_token():
        """Returns (raw token for the link, hash to store)."""
        raw = secrets.token_urlsafe(32)
        return raw, hash_token(raw)

    @property
    def is_pending(self):
        return self.accepted_at is None and self.revoked_at is None and self.expires_at > timezone.now()

    def __str__(self):
        return f"Invitation for {self.email} to {self.workspace}"
