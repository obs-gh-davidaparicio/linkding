from typing import Union
from contextlib import nullcontext

from django.contrib.auth.models import User
from django.utils import timezone

from bookmarks.models import Bookmark, parse_tag_string
from bookmarks.services.tags import get_or_create_tags
from bookmarks.services import website_loader
from bookmarks.services import tasks

# OpenTelemetry imports
try:
    import otel
    from opentelemetry import trace
    from opentelemetry import metrics
    tracer = otel.get_tracer()
    meter = otel.get_meter()
    logger = otel.get_logger()

    # Create metrics
    bookmark_operations_counter = meter.create_counter(
        "bookmark_operations_total",
        description="Total number of bookmark operations",
        unit="1"
    ) if meter else None
    bookmark_operation_duration = meter.create_histogram(
        "bookmark_operation_duration_seconds",
        description="Duration of bookmark operations",
        unit="s"
    ) if meter else None
except ImportError:
    # OpenTelemetry not available, use no-op implementations
    tracer = None
    meter = None
    logger = None
    bookmark_operations_counter = None
    bookmark_operation_duration = None


def create_bookmark(bookmark: Bookmark, tag_string: str, current_user: User):
    with tracer.start_as_current_span("bookmark.create") if tracer else nullcontext():
        if tracer:
            span = trace.get_current_span()
            span.set_attributes({
                "bookmark.url": bookmark.url,
                "bookmark.title": bookmark.title or "",
                "user.id": current_user.id,
                "user.username": current_user.username,
                "tags.count": len(tag_string.split()) if tag_string else 0
            })

        if bookmark_operations_counter:
            bookmark_operations_counter.add(1, {"operation": "create", "user_id": str(current_user.id)})

        start_time = timezone.now()

        try:
            # If URL is already bookmarked, then update it
            existing_bookmark: Bookmark = Bookmark.objects.filter(owner=current_user, url=bookmark.url).first()

            if existing_bookmark is not None:
                if tracer:
                    span.set_attribute("bookmark.existing", True)
                    span.add_event("bookmark.merge_existing")
                _merge_bookmark_data(bookmark, existing_bookmark)
                return update_bookmark(existing_bookmark, tag_string, current_user)

            if tracer:
                span.set_attribute("bookmark.existing", False)

            # Update website info
            _update_website_metadata(bookmark)
            # Set currently logged in user as owner
            bookmark.owner = current_user
            # Set dates
            bookmark.date_added = timezone.now()
            bookmark.date_modified = timezone.now()
            bookmark.save()
            # Update tag list
            _update_bookmark_tags(bookmark, tag_string, current_user)
            bookmark.save()
            # Create snapshot on web archive
            tasks.create_web_archive_snapshot(current_user, bookmark, False)

            if tracer:
                span.set_attribute("bookmark.id", bookmark.id)
                span.set_status(trace.Status(trace.StatusCode.OK))

            if logger:
                logger.info("Bookmark created successfully", extra={
                    "bookmark_id": bookmark.id,
                    "user_id": current_user.id,
                    "url": bookmark.url
                })

            return bookmark

        except Exception as e:
            if tracer:
                span.record_exception(e)
                span.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))
            if logger:
                logger.error("Failed to create bookmark", extra={
                    "user_id": current_user.id,
                    "url": bookmark.url,
                    "error": str(e)
                })
            raise
        finally:
            if bookmark_operation_duration:
                duration = (timezone.now() - start_time).total_seconds()
                bookmark_operation_duration.record(duration, {"operation": "create", "user_id": str(current_user.id)})


def update_bookmark(bookmark: Bookmark, tag_string, current_user: User):
    with tracer.start_as_current_span("bookmark.update") if tracer else nullcontext():
        if tracer:
            span = trace.get_current_span()
            span.set_attributes({
                "bookmark.id": bookmark.id,
                "bookmark.url": bookmark.url,
                "bookmark.title": bookmark.title or "",
                "user.id": current_user.id,
                "user.username": current_user.username,
                "tags.count": len(tag_string.split()) if tag_string else 0
            })

        if bookmark_operations_counter:
            bookmark_operations_counter.add(1, {"operation": "update", "user_id": str(current_user.id)})

        start_time = timezone.now()

        try:
            # Detect URL change
            original_bookmark = Bookmark.objects.get(id=bookmark.id)
            has_url_changed = original_bookmark.url != bookmark.url

            if tracer:
                span.set_attribute("bookmark.url_changed", has_url_changed)

            # Update tag list
            _update_bookmark_tags(bookmark, tag_string, current_user)
            # Update dates
            bookmark.date_modified = timezone.now()
            bookmark.save()
            if has_url_changed:
                if tracer:
                    span.add_event("bookmark.url_changed", {"old_url": original_bookmark.url, "new_url": bookmark.url})
                # Update web archive snapshot, if URL changed
                tasks.create_web_archive_snapshot(current_user, bookmark, True)
                # Only update website metadata if URL changed
                _update_website_metadata(bookmark)

            if tracer:
                span.set_status(trace.Status(trace.StatusCode.OK))

            if logger:
                logger.info("Bookmark updated successfully", extra={
                    "bookmark_id": bookmark.id,
                    "user_id": current_user.id,
                    "url_changed": has_url_changed
                })

            return bookmark

        except Exception as e:
            if tracer:
                span.record_exception(e)
                span.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))
            if logger:
                logger.error("Failed to update bookmark", extra={
                    "bookmark_id": bookmark.id,
                    "user_id": current_user.id,
                    "error": str(e)
                })
            raise
        finally:
            if bookmark_operation_duration:
                duration = (timezone.now() - start_time).total_seconds()
                bookmark_operation_duration.record(duration, {"operation": "update", "user_id": str(current_user.id)})


def archive_bookmark(bookmark: Bookmark):
    with tracer.start_as_current_span("bookmark.archive") if tracer else nullcontext():
        if tracer:
            span = trace.get_current_span()
            span.set_attributes({
                "bookmark.id": bookmark.id,
                "bookmark.url": bookmark.url,
                "bookmark.title": bookmark.title or ""
            })

        if bookmark_operations_counter:
            bookmark_operations_counter.add(1, {"operation": "archive"})

        try:
            bookmark.is_archived = True
            bookmark.date_modified = timezone.now()
            bookmark.save()

            if tracer:
                span.set_status(trace.Status(trace.StatusCode.OK))

            if logger:
                logger.info("Bookmark archived successfully", extra={"bookmark_id": bookmark.id})

            return bookmark

        except Exception as e:
            if tracer:
                span.record_exception(e)
                span.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))
            if logger:
                logger.error("Failed to archive bookmark", extra={"bookmark_id": bookmark.id, "error": str(e)})
            raise


def archive_bookmarks(bookmark_ids: [Union[int, str]], current_user: User):
    sanitized_bookmark_ids = _sanitize_id_list(bookmark_ids)
    bookmarks = Bookmark.objects.filter(owner=current_user, id__in=sanitized_bookmark_ids)

    bookmarks.update(is_archived=True, date_modified=timezone.now())


def unarchive_bookmark(bookmark: Bookmark):
    with tracer.start_as_current_span("bookmark.unarchive") if tracer else nullcontext():
        if tracer:
            span = trace.get_current_span()
            span.set_attributes({
                "bookmark.id": bookmark.id,
                "bookmark.url": bookmark.url,
                "bookmark.title": bookmark.title or ""
            })

        if bookmark_operations_counter:
            bookmark_operations_counter.add(1, {"operation": "unarchive"})

        try:
            bookmark.is_archived = False
            bookmark.date_modified = timezone.now()
            bookmark.save()

            if tracer:
                span.set_status(trace.Status(trace.StatusCode.OK))

            if logger:
                logger.info("Bookmark unarchived successfully", extra={"bookmark_id": bookmark.id})

            return bookmark

        except Exception as e:
            if tracer:
                span.record_exception(e)
                span.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))
            if logger:
                logger.error("Failed to unarchive bookmark", extra={"bookmark_id": bookmark.id, "error": str(e)})
            raise


def unarchive_bookmarks(bookmark_ids: [Union[int, str]], current_user: User):
    sanitized_bookmark_ids = _sanitize_id_list(bookmark_ids)
    bookmarks = Bookmark.objects.filter(owner=current_user, id__in=sanitized_bookmark_ids)

    bookmarks.update(is_archived=False, date_modified=timezone.now())


def delete_bookmarks(bookmark_ids: [Union[int, str]], current_user: User):
    sanitized_bookmark_ids = _sanitize_id_list(bookmark_ids)
    bookmarks = Bookmark.objects.filter(owner=current_user, id__in=sanitized_bookmark_ids)

    bookmarks.delete()


def tag_bookmarks(bookmark_ids: [Union[int, str]], tag_string: str, current_user: User):
    sanitized_bookmark_ids = _sanitize_id_list(bookmark_ids)
    bookmarks = Bookmark.objects.filter(owner=current_user, id__in=sanitized_bookmark_ids)
    tag_names = parse_tag_string(tag_string)
    tags = get_or_create_tags(tag_names, current_user)

    for bookmark in bookmarks:
        bookmark.tags.add(*tags)
        bookmark.date_modified = timezone.now()

    Bookmark.objects.bulk_update(bookmarks, ['date_modified'])


def untag_bookmarks(bookmark_ids: [Union[int, str]], tag_string: str, current_user: User):
    sanitized_bookmark_ids = _sanitize_id_list(bookmark_ids)
    bookmarks = Bookmark.objects.filter(owner=current_user, id__in=sanitized_bookmark_ids)
    tag_names = parse_tag_string(tag_string)
    tags = get_or_create_tags(tag_names, current_user)

    for bookmark in bookmarks:
        bookmark.tags.remove(*tags)
        bookmark.date_modified = timezone.now()

    Bookmark.objects.bulk_update(bookmarks, ['date_modified'])


def _merge_bookmark_data(from_bookmark: Bookmark, to_bookmark: Bookmark):
    to_bookmark.title = from_bookmark.title
    to_bookmark.description = from_bookmark.description
    to_bookmark.unread = from_bookmark.unread
    to_bookmark.shared = from_bookmark.shared


def _update_website_metadata(bookmark: Bookmark):
    metadata = website_loader.load_website_metadata(bookmark.url)
    bookmark.website_title = metadata.title
    bookmark.website_description = metadata.description


def _update_bookmark_tags(bookmark: Bookmark, tag_string: str, user: User):
    tag_names = parse_tag_string(tag_string)
    tags = get_or_create_tags(tag_names, user)
    bookmark.tags.set(tags)


def _sanitize_id_list(bookmark_ids: [Union[int, str]]) -> [int]:
    # Convert string ids to int if necessary
    return [int(bm_id) if isinstance(bm_id, str) else bm_id for bm_id in bookmark_ids]
