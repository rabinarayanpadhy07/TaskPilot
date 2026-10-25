from django.contrib import admin

from sprints.models import Sprint


@admin.register(Sprint)
class SprintAdmin(admin.ModelAdmin):
    list_display = ["name", "project", "status", "start_date", "end_date"]
    list_filter = ["status"]
