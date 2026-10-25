import mimetypes
from pathlib import PurePath

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.urls import reverse
from rest_framework import serializers

from issues.models import Attachment, Comment, Issue
from sprints.models import Sprint
from workspaces import access
from workspaces.roles import ProjectPerm

User = get_user_model()

ALLOWED_PARENT_TYPES = {
    Issue.Type.EPIC: set(),
    Issue.Type.SUBTASK: {Issue.Type.STORY, Issue.Type.TASK, Issue.Type.BUG},
}

# Uploads: allow-list by extension AND declared type (SVG/HTML excluded: no active content)
ATTACHMENT_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
    ".pdf": "application/pdf", ".txt": "text/plain", ".csv": "text/csv", ".zip": "application/zip",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
ATTACHMENT_SIGNER_SALT = "taskpilot.attachment"


def allowed_parent_types(issue_type):
    return ALLOWED_PARENT_TYPES.get(issue_type, {Issue.Type.EPIC})


def _perms(context, project):
    """Caller's permissions in `project`, from the per-request map when the view built one."""
    cached = context.get("project_perms")
    if cached is not None and project.pk in cached:
        return cached[project.pk]
    return access.project_permissions(context["membership"], project)


class IssueSerializer(serializers.ModelSerializer):
    """
    Related fields are limited to the caller's workspace and visible projects,
    so ids from another workspace fail validation exactly like missing ids.
    """

    project = serializers.PrimaryKeyRelatedField(queryset=Issue.objects.none())
    assignee = serializers.PrimaryKeyRelatedField(queryset=User.objects.none(), allow_null=True, required=False)
    parent = serializers.PrimaryKeyRelatedField(queryset=Issue.objects.none(), allow_null=True, required=False)
    sprint = serializers.PrimaryKeyRelatedField(queryset=Sprint.objects.none(), allow_null=True, required=False)
    title = serializers.CharField(max_length=255, trim_whitespace=True)
    labels = serializers.ListField(child=serializers.CharField(max_length=40, trim_whitespace=True), required=False, max_length=20)
    comment_count = serializers.SerializerMethodField()
    attachment_count = serializers.SerializerMethodField()
    permissions = serializers.SerializerMethodField()

    class Meta:
        model = Issue
        fields = [
            "id", "key", "project", "issue_type", "title", "description", "status", "priority",
            "assignee", "reporter", "labels", "due_date", "story_points", "parent", "sprint", "rank",
            "created_at", "updated_at", "completed_at", "comment_count", "attachment_count", "permissions",
        ]
        read_only_fields = ["id", "key", "reporter", "rank", "created_at", "updated_at", "completed_at"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        membership = self.context.get("membership")
        if membership is None:
            return
        projects = access.visible_projects(membership)
        self.fields["project"].queryset = projects
        self.fields["assignee"].queryset = User.objects.filter(workspace_memberships__workspace_id=membership.workspace_id)
        self.fields["parent"].queryset = Issue.objects.filter(project__in=projects)
        self.fields["sprint"].queryset = Sprint.objects.filter(project__in=projects)
        if isinstance(self.instance, Issue):
            self.fields["project"].read_only = True  # moving between projects isn't supported

    def validate_labels(self, value):
        cleaned = []
        for label in value:
            label = label.strip().lower()
            if label and label not in cleaned:
                cleaned.append(label)
        return cleaned

    def validate(self, attrs):
        instance = self.instance if isinstance(self.instance, Issue) else None
        project = attrs.get("project", instance.project if instance else None)
        issue_type = attrs.get("issue_type", instance.issue_type if instance else Issue.Type.TASK)
        parent = attrs.get("parent", instance.parent if instance else None)
        sprint = attrs.get("sprint", instance.sprint if instance else None)
        errors = {}

        if parent is None and issue_type == Issue.Type.SUBTASK:
            errors["parent"] = ["A subtask needs a parent issue."]
        elif parent is not None:
            if instance and parent.pk == instance.pk:
                errors["parent"] = ["An issue can't be its own parent."]
            elif parent.issue_type not in allowed_parent_types(issue_type):
                errors["parent"] = [
                    "Epics can't have a parent." if issue_type == Issue.Type.EPIC else f"A {issue_type} can't be a child of a {parent.issue_type}."
                ]
            elif parent.project_id != project.pk:
                errors["parent"] = ["The parent must be in the same project."]
        if instance and "issue_type" in attrs and attrs["issue_type"] != instance.issue_type:
            # Existing children must still be allowed under the new type
            child_types = set(instance.children.values_list("issue_type", flat=True))
            if any(attrs["issue_type"] not in allowed_parent_types(t) for t in child_types):
                errors["issue_type"] = ["This issue has child issues that can't belong to that type. Move them first."]

        if sprint is not None and ("sprint" in attrs or "issue_type" in attrs):
            if sprint.project_id != project.pk:
                errors["sprint"] = ["Sprint not found in this project."]
            elif sprint.status == Sprint.Status.COMPLETED and "sprint" in attrs:
                errors["sprint"] = ["Issues can't be added to a completed sprint."]
            elif issue_type not in Issue.PLANNABLE_TYPES and "sprint" in attrs:
                errors["sprint"] = ["Only stories, tasks and bugs can be planned in a sprint."]

        if errors:
            raise serializers.ValidationError(errors)
        return attrs

    def get_comment_count(self, issue) -> int:
        value = getattr(issue, "num_comments", None)
        return value if value is not None else issue.comments.count()

    def get_attachment_count(self, issue) -> int:
        value = getattr(issue, "num_attachments", None)
        return value if value is not None else issue.attachments.count()

    def get_permissions(self, issue) -> dict[str, bool]:
        perms = _perms(self.context, issue.project)
        return {
            "can_edit": ProjectPerm.ISSUES_WRITE in perms,
            "can_comment": ProjectPerm.COMMENT in perms,
            "can_delete": ProjectPerm.ISSUES_DELETE in perms,
        }


class MoveSerializer(serializers.Serializer):
    """Drag-and-drop: optional field changes + the new neighbours (issue keys)."""

    status = serializers.ChoiceField(choices=Issue.Status.choices, required=False)
    sprint = serializers.IntegerField(allow_null=True, required=False)
    parent = serializers.IntegerField(allow_null=True, required=False)
    before = serializers.CharField(allow_null=True, required=False, max_length=24)
    after = serializers.CharField(allow_null=True, required=False, max_length=24)


class BulkUpdateSerializer(serializers.Serializer):
    keys = serializers.ListField(child=serializers.CharField(max_length=24), min_length=1, max_length=100)
    changes = serializers.DictField()

    ALLOWED = {"status", "sprint", "assignee", "priority"}

    def validate_changes(self, value):
        unknown = set(value) - self.ALLOWED
        if unknown:
            raise serializers.ValidationError(f"Unsupported fields: {', '.join(sorted(unknown))}.")
        if not value:
            raise serializers.ValidationError("Nothing to change.")
        return value


class CommentSerializer(serializers.ModelSerializer):
    body = serializers.CharField(max_length=5000, trim_whitespace=True)
    permissions = serializers.SerializerMethodField()

    class Meta:
        model = Comment
        fields = ["id", "issue", "author", "body", "created_at", "edited_at", "permissions"]
        read_only_fields = ["id", "issue", "author", "created_at", "edited_at"]

    def get_permissions(self, comment) -> dict[str, bool]:
        return comment_permissions(self.context["membership"], comment, _perms(self.context, comment.issue.project))


def comment_permissions(membership, comment, perms):
    is_author = membership is not None and comment.author_id == membership.user_id
    can_comment = ProjectPerm.COMMENT in perms
    return {
        "can_edit": is_author and can_comment,
        "can_delete": (is_author and can_comment) or ProjectPerm.ISSUES_DELETE in perms,
    }


def signed_attachment_url(request, attachment, user):
    token = signing.TimestampSigner(salt=ATTACHMENT_SIGNER_SALT).sign(f"{attachment.pk}:{user.pk}")
    return request.build_absolute_uri(reverse("attachment-download", args=[token]))


class AttachmentSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()
    file = serializers.FileField(write_only=True)

    class Meta:
        model = Attachment
        fields = ["id", "file", "file_name", "size", "content_type", "url", "uploaded_by", "created_at"]
        read_only_fields = ["id", "file_name", "size", "content_type", "url", "uploaded_by", "created_at"]

    def get_url(self, attachment) -> str:
        request = self.context["request"]
        return signed_attachment_url(request, attachment, request.user)

    def validate_file(self, upload):
        if upload.size > settings.ATTACHMENT_MAX_BYTES:
            raise serializers.ValidationError(f"Files can be at most {settings.ATTACHMENT_MAX_BYTES // (1024 * 1024)} MB.")
        extension = PurePath(upload.name).suffix.lower()
        expected = ATTACHMENT_TYPES.get(extension)
        declared = (upload.content_type or "").split(";")[0].strip().lower()
        if expected is None:
            raise serializers.ValidationError("This file type isn't allowed.")
        # Browsers sometimes send octet-stream; otherwise the declared type must match the extension
        if declared not in ("", "application/octet-stream", expected) and declared != mimetypes.guess_type(upload.name)[0]:
            raise serializers.ValidationError("The file's type doesn't match its extension.")
        return upload
