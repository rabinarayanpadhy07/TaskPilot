from rest_framework.pagination import PageNumberPagination


class StandardPagination(PageNumberPagination):
    """
    `?page=2&page_size=100`. Responses: { count, next, previous, results }.
    The frontend follows `next` for screens that need a full list (e.g. a
    project's board), so page_size is capped to keep each query bounded.
    """

    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 200
