import logging
from contextlib import nullcontext

import waybackpy
from background_task import background
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import User
from waybackpy.exceptions import WaybackError, TooManyRequestsError, NoCDXRecordFound

import bookmarks.services.wayback
from bookmarks.models import Bookmark, UserProfile
from bookmarks.services.website_loader import DEFAULT_USER_AGENT

logger = logging.getLogger(__name__)

# OpenTelemetry imports
try:
    import otel
    from opentelemetry import trace
    from opentelemetry import metrics
    tracer = otel.get_tracer()
    meter = otel.get_meter()
    otel_logger = otel.get_logger()

    # Create metrics
    web_archive_operations_counter = meter.create_counter(
        "web_archive_operations_total",
        description="Total number of web archive operations",
        unit="1"
    ) if meter else None
    web_archive_operation_duration = meter.create_histogram(
        "web_archive_operation_duration_seconds",
        description="Duration of web archive operations",
        unit="s"
    ) if meter else None
except ImportError:
    # OpenTelemetry not available, use no-op implementations
    tracer = None
    meter = None
    otel_logger = None
    web_archive_operations_counter = None
    web_archive_operation_duration = None


def is_web_archive_integration_active(user: User) -> bool:
    background_tasks_enabled = not settings.LD_DISABLE_BACKGROUND_TASKS
    web_archive_integration_enabled = \
        user.profile.web_archive_integration == UserProfile.WEB_ARCHIVE_INTEGRATION_ENABLED

    return background_tasks_enabled and web_archive_integration_enabled


def create_web_archive_snapshot(user: User, bookmark: Bookmark, force_update: bool):
    if is_web_archive_integration_active(user):
        _create_web_archive_snapshot_task(bookmark.id, force_update)


def _load_newest_snapshot(bookmark: Bookmark):
    try:
        logger.info(f'Load existing snapshot for bookmark. url={bookmark.url}')
        cdx_api = bookmarks.services.wayback.CustomWaybackMachineCDXServerAPI(bookmark.url)
        existing_snapshot = cdx_api.newest()

        if existing_snapshot:
            bookmark.web_archive_snapshot_url = existing_snapshot.archive_url
            bookmark.save()
            logger.info(f'Using newest snapshot. url={bookmark.url} from={existing_snapshot.datetime_timestamp}')

    except NoCDXRecordFound:
        logger.info(f'Could not find any snapshots for bookmark. url={bookmark.url}')
    except WaybackError as error:
        logger.error(f'Failed to load existing snapshot. url={bookmark.url}', exc_info=error)


def _create_snapshot(bookmark: Bookmark):
    logger.info(f'Create new snapshot for bookmark. url={bookmark.url}...')
    archive = waybackpy.WaybackMachineSaveAPI(bookmark.url, DEFAULT_USER_AGENT, max_tries=1)
    archive.save()
    bookmark.web_archive_snapshot_url = archive.archive_url
    bookmark.save()
    logger.info(f'Successfully created new snapshot for bookmark:. url={bookmark.url}')


@background()
def _create_web_archive_snapshot_task(bookmark_id: int, force_update: bool):
    with tracer.start_as_current_span("web_archive.create_snapshot_task") if tracer else nullcontext():
        if tracer:
            span = trace.get_current_span()
            span.set_attributes({
                "bookmark.id": bookmark_id,
                "web_archive.force_update": force_update
            })

        if web_archive_operations_counter:
            web_archive_operations_counter.add(1, {"operation": "create_snapshot"})

        try:
            bookmark = Bookmark.objects.get(id=bookmark_id)

            if tracer:
                span.set_attributes({
                    "bookmark.url": bookmark.url,
                    "bookmark.title": bookmark.title or ""
                })
        except Bookmark.DoesNotExist:
            if tracer:
                span.set_status(trace.Status(trace.StatusCode.ERROR, "Bookmark not found"))
            if otel_logger:
                otel_logger.warning("Bookmark not found for web archive task", extra={"bookmark_id": bookmark_id})
            return

        # Skip if snapshot exists and update is not explicitly requested
        if bookmark.web_archive_snapshot_url and not force_update:
            if tracer:
                span.add_event("snapshot_exists_skipping")
                span.set_status(trace.Status(trace.StatusCode.OK))
            return

        # Create new snapshot
        try:
            _create_snapshot(bookmark)
            if tracer:
                span.set_status(trace.Status(trace.StatusCode.OK))
                span.add_event("snapshot_created_successfully")
            return
        except TooManyRequestsError:
            if tracer:
                span.add_event("rate_limited_fallback_to_newest")
            logger.error(
                f'Failed to create snapshot due to rate limiting, trying to load newest snapshot as fallback. url={bookmark.url}')
        except WaybackError as error:
            if tracer:
                span.record_exception(error)
                span.add_event("wayback_error_fallback_to_newest")
            logger.error(f'Failed to create snapshot, trying to load newest snapshot as fallback. url={bookmark.url}', exc_info=error)

        # Load the newest snapshot as fallback
        _load_newest_snapshot(bookmark)
        if tracer:
            span.add_event("fallback_snapshot_loaded")


@background()
def _load_web_archive_snapshot_task(bookmark_id: int):
    try:
        bookmark = Bookmark.objects.get(id=bookmark_id)
    except Bookmark.DoesNotExist:
        return
    # Skip if snapshot exists
    if bookmark.web_archive_snapshot_url:
        return
    # Load the newest snapshot
    _load_newest_snapshot(bookmark)


def schedule_bookmarks_without_snapshots(user: User):
    if is_web_archive_integration_active(user):
        _schedule_bookmarks_without_snapshots_task(user.id)


@background()
def _schedule_bookmarks_without_snapshots_task(user_id: int):
    user = get_user_model().objects.get(id=user_id)
    bookmarks_without_snapshots = Bookmark.objects.filter(web_archive_snapshot_url__exact='', owner=user)

    for bookmark in bookmarks_without_snapshots:
        # To prevent rate limit errors from the Wayback API only try to load the latest snapshots instead of creating
        # new ones when processing bookmarks in bulk
        _load_web_archive_snapshot_task(bookmark.id)
