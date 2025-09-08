from contextlib import nullcontext

from django.urls import reverse
from rest_framework import viewsets, mixins, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.routers import DefaultRouter

from bookmarks import queries
from bookmarks.api.serializers import BookmarkSerializer, TagSerializer
from bookmarks.models import Bookmark, BookmarkFilters, Tag, User
from bookmarks.services.bookmarks import archive_bookmark, unarchive_bookmark
from bookmarks.services.website_loader import load_website_metadata

# OpenTelemetry imports
try:
    import otel
    from opentelemetry import trace
    from opentelemetry import metrics
    tracer = otel.get_tracer()
    meter = otel.get_meter()
    logger = otel.get_logger()

    # Create metrics
    api_requests_counter = meter.create_counter(
        "api_requests_total",
        description="Total number of API requests",
        unit="1"
    ) if meter else None
    api_request_duration = meter.create_histogram(
        "api_request_duration_seconds",
        description="Duration of API requests",
        unit="s"
    ) if meter else None
except ImportError:
    # OpenTelemetry not available, use no-op implementations
    tracer = None
    meter = None
    logger = None
    api_requests_counter = None
    api_request_duration = None


class BookmarkViewSet(viewsets.GenericViewSet,
                      mixins.ListModelMixin,
                      mixins.RetrieveModelMixin,
                      mixins.CreateModelMixin,
                      mixins.UpdateModelMixin,
                      mixins.DestroyModelMixin):
    serializer_class = BookmarkSerializer

    def get_queryset(self):
        user = self.request.user
        # For list action, use query set that applies search and tag projections
        if self.action == 'list':
            query_string = self.request.GET.get('q')
            return queries.query_bookmarks(user, query_string)

        # For single entity actions use default query set without projections
        return Bookmark.objects.all().filter(owner=user)

    def get_serializer_context(self):
        return {'user': self.request.user}

    @action(methods=['get'], detail=False)
    def archived(self, request):
        user = request.user
        query_string = request.GET.get('q')
        query_set = queries.query_archived_bookmarks(user, query_string)
        page = self.paginate_queryset(query_set)
        serializer = self.get_serializer_class()
        data = serializer(page, many=True).data
        return self.get_paginated_response(data)

    @action(methods=['get'], detail=False)
    def shared(self, request):
        filters = BookmarkFilters(request)
        user = User.objects.filter(username=filters.user).first()
        query_set = queries.query_shared_bookmarks(user, filters.query)
        page = self.paginate_queryset(query_set)
        serializer = self.get_serializer_class()
        data = serializer(page, many=True).data
        return self.get_paginated_response(data)

    @action(methods=['post'], detail=True)
    def archive(self, request, pk):
        bookmark = self.get_object()
        archive_bookmark(bookmark)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(methods=['post'], detail=True)
    def unarchive(self, request, pk):
        bookmark = self.get_object()
        unarchive_bookmark(bookmark)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(methods=['get'], detail=False)
    def check(self, request):
        with tracer.start_as_current_span("api.bookmark.check") if tracer else nullcontext():
            if tracer:
                span = trace.get_current_span()
                url = request.GET.get('url')
                span.set_attributes({
                    "api.endpoint": "bookmark.check",
                    "api.method": "GET",
                    "user.id": request.user.id,
                    "user.username": request.user.username,
                    "bookmark.url": url or ""
                })

            if api_requests_counter:
                api_requests_counter.add(1, {"endpoint": "bookmark.check", "method": "GET"})

            try:
                url = request.GET.get('url')
                bookmark = Bookmark.objects.filter(owner=request.user, url=url).first()
                existing_bookmark_data = None

                if bookmark is not None:
                    existing_bookmark_data = {
                        'id': bookmark.id,
                        'edit_url': reverse('bookmarks:edit', args=[bookmark.id])
                    }
                    if tracer:
                        span.set_attribute("bookmark.exists", True)
                        span.set_attribute("bookmark.id", bookmark.id)
                else:
                    if tracer:
                        span.set_attribute("bookmark.exists", False)

                metadata = load_website_metadata(url)

                if tracer:
                    span.set_status(trace.Status(trace.StatusCode.OK))

                if logger:
                    logger.info("Bookmark check completed", extra={
                        "user_id": request.user.id,
                        "url": url,
                        "bookmark_exists": bookmark is not None
                    })

                return Response({
                    'bookmark': existing_bookmark_data,
                    'metadata': metadata.to_dict()
                }, status=status.HTTP_200_OK)

            except Exception as e:
                if tracer:
                    span.record_exception(e)
                    span.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))
                if logger:
                    logger.error("Bookmark check failed", extra={
                        "user_id": request.user.id,
                        "url": url,
                        "error": str(e)
                    })
                raise


class TagViewSet(viewsets.GenericViewSet,
                 mixins.ListModelMixin,
                 mixins.RetrieveModelMixin,
                 mixins.CreateModelMixin):
    serializer_class = TagSerializer

    def get_queryset(self):
        user = self.request.user
        return Tag.objects.all().filter(owner=user)

    def get_serializer_context(self):
        return {'user': self.request.user}


router = DefaultRouter()
router.register(r'bookmarks', BookmarkViewSet, basename='bookmark')
router.register(r'tags', TagViewSet, basename='tag')
