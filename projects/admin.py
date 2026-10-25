from django.contrib import admin

from projects.models import Project, ProjectMembership


class ProjectMembershipInline(admin.TabularInline):
    model = ProjectMembership
    extra = 0
    autocomplete_fields = ["user"]


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ["key", "name", "workspace", "visibility", "status"]
    list_filter = ["visibility", "status"]
    search_fields = ["key", "name"]
    inlines = [ProjectMembershipInline]
