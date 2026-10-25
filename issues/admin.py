from django.contrib import admin

from issues.models import Attachment, Comment, Issue


@admin.register(Issue)
class IssueAdmin(admin.ModelAdmin):
    list_display = ["key", "title", "issue_type", "status", "assignee"]
    list_filter = ["issue_type", "status"]
    search_fields = ["key", "title"]
    raw_id_fields = ["project", "assignee", "reporter", "parent", "sprint"]


@admin.register(Comment)
class CommentAdmin(admin.ModelAdmin):
    list_display = ["issue", "author", "created_at"]
    raw_id_fields = ["issue", "author"]


@admin.register(Attachment)
class AttachmentAdmin(admin.ModelAdmin):
    list_display = ["file_name", "issue", "size", "created_at"]
    raw_id_fields = ["issue", "uploaded_by"]
