import django_filters
from django.db.models import Q

from issues.models import Issue


class NumberOrNoneFilter(django_filters.CharFilter):
    """`?sprint=12` or `?sprint=none` (unset). Anything else matches nothing."""

    def filter(self, qs, value):
        if value in (None, ""):
            return qs
        if value == "none":
            return qs.filter(**{f"{self.field_name}__isnull": True})
        if not value.isdigit():
            return qs.none()
        return qs.filter(**{self.field_name: int(value)})


class IssueFilter(django_filters.FilterSet):
    project = NumberOrNoneFilter(field_name="project_id")
    assignee = NumberOrNoneFilter(field_name="assignee_id")
    sprint = NumberOrNoneFilter(field_name="sprint_id")
    parent = NumberOrNoneFilter(field_name="parent_id")
    status = django_filters.MultipleChoiceFilter(choices=Issue.Status.choices)
    issue_type = django_filters.MultipleChoiceFilter(choices=Issue.Type.choices)
    priority = django_filters.MultipleChoiceFilter(choices=Issue.Priority.choices)
    label = django_filters.CharFilter(method="filter_label")
    q = django_filters.CharFilter(method="filter_q")
    updated_after = django_filters.IsoDateTimeFilter(field_name="updated_at", lookup_expr="gte")
    due_before = django_filters.DateFilter(field_name="due_date", lookup_expr="lte")
    open = django_filters.BooleanFilter(method="filter_open")

    class Meta:
        model = Issue
        fields = []

    def filter_label(self, qs, name, value):
        return qs.filter(labels__contains=[value.strip().lower()])

    def filter_open(self, qs, name, value):
        return qs.exclude(status=Issue.Status.DONE) if value else qs.filter(status=Issue.Status.DONE)

    def filter_q(self, qs, name, value):
        value = value.strip()
        return qs.filter(Q(title__icontains=value) | Q(key__iexact=value)) if value else qs
