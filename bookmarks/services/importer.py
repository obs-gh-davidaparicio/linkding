import logging
from dataclasses import dataclass
from typing import List

from django.contrib.auth.models import User
from django.utils import timezone
from opentelemetry import trace
from otel import get_business_meter

from bookmarks.models import Bookmark, Tag, parse_tag_string
from bookmarks.services import tasks
from bookmarks.services.parser import parse, NetscapeBookmark
from bookmarks.utils import parse_timestamp

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
meter = get_business_meter()

# Business metrics
import_operations_counter = meter.create_counter(
    name="linkding_import_operations_total",
    description="Total number of import operations",
    unit="1"
)

import_duration_histogram = meter.create_histogram(
    name="linkding_import_duration_seconds",
    description="Duration of import operations in seconds",
    unit="s"
)


@dataclass
class ImportResult:
    total: int = 0
    success: int = 0
    failed: int = 0


class TagCache:
    def __init__(self, user: User):
        self.user = user
        self.cache = dict()
        # Init cache with all existing tags for that user
        tags = Tag.objects.filter(owner=user)
        for tag in tags:
            self.put(tag)

    def get(self, tag_name: str):
        tag_name_lowercase = tag_name.lower()
        if tag_name_lowercase in self.cache:
            return self.cache[tag_name_lowercase]
        else:
            return None

    def get_all(self, tag_names: List[str]):
        result = []
        for tag_name in tag_names:
            tag = self.get(tag_name)
            # Prevent returning duplicates
            if not (tag in result):
                result.append(tag)

        return result

    def put(self, tag: Tag):
        self.cache[tag.name.lower()] = tag


def import_netscape_html(html: str, user: User):
    with tracer.start_as_current_span("import_netscape_html") as span:
        span.set_attribute("user.id", user.id)
        span.set_attribute("user.username", user.username)
        span.set_attribute("import.html_size", len(html))

        result = ImportResult()
        import_start = timezone.now()

        try:
            netscape_bookmarks = parse(html)
            span.set_attribute("import.parsed_bookmarks", len(netscape_bookmarks))
        except Exception as e:
            span.record_exception(e)
            span.set_status(trace.Status(trace.StatusCode.ERROR, "Failed to parse HTML"))
            logging.exception('Could not read bookmarks file.')
            raise

        parse_end = timezone.now()
        parse_duration = (parse_end - import_start).total_seconds()
        span.set_attribute("import.parse_duration_seconds", parse_duration)
        logger.debug(f'Parse duration: {parse_end - import_start}')

        # Create and cache all tags beforehand
        _create_missing_tags(netscape_bookmarks, user)
        tag_cache = TagCache(user)

        # Split bookmarks to import into batches, to keep memory usage for bulk operations manageable
        batches = _get_batches(netscape_bookmarks, 200)
        span.set_attribute("import.batch_count", len(batches))

        for batch_index, batch in enumerate(batches):
            with tracer.start_as_current_span("import_batch") as batch_span:
                batch_span.set_attribute("import.batch_index", batch_index)
                batch_span.set_attribute("import.batch_size", len(batch))
                _import_batch(batch, user, tag_cache, result)

        # Create snapshots for newly imported bookmarks
        tasks.schedule_bookmarks_without_snapshots(user)

        end = timezone.now()
        total_duration = (end - import_start).total_seconds()
        span.set_attribute("import.total_duration_seconds", total_duration)
        span.set_attribute("import.result.total", result.total)
        span.set_attribute("import.result.success", result.success)
        span.set_attribute("import.result.failed", result.failed)
        logger.debug(f'Import duration: {end - import_start}')

        # Record business metrics
        import_operations_counter.add(1, {
            "user_id": str(user.id),
            "success": str(result.success),
            "failed": str(result.failed)
        })
        import_duration_histogram.record(total_duration, {
            "user_id": str(user.id),
            "bookmark_count": str(result.total)
        })

        return result


def _create_missing_tags(netscape_bookmarks: List[NetscapeBookmark], user: User):
    tag_cache = TagCache(user)
    tags_to_create = []

    for netscape_bookmark in netscape_bookmarks:
        tag_names = parse_tag_string(netscape_bookmark.tag_string)
        for tag_name in tag_names:
            tag = tag_cache.get(tag_name)
            if not tag:
                tag = Tag(name=tag_name, owner=user)
                tag.date_added = timezone.now()
                tags_to_create.append(tag)
                tag_cache.put(tag)

    Tag.objects.bulk_create(tags_to_create)


def _get_batches(items: List, batch_size: int):
    batches = []
    offset = 0
    num_items = len(items)

    while offset < num_items:
        batch = items[offset:min(offset + batch_size, num_items)]
        if len(batch) > 0:
            batches.append(batch)
        offset = offset + batch_size

    return batches


def _import_batch(netscape_bookmarks: List[NetscapeBookmark], user: User, tag_cache: TagCache, result: ImportResult):
    # Query existing bookmarks
    batch_urls = [bookmark.href for bookmark in netscape_bookmarks]
    existing_bookmarks = Bookmark.objects.filter(owner=user, url__in=batch_urls)

    # Create or update bookmarks from parsed Netscape bookmarks
    bookmarks_to_create = []
    bookmarks_to_update = []

    for netscape_bookmark in netscape_bookmarks:
        result.total = result.total + 1
        try:
            # Lookup existing bookmark by URL, or create new bookmark if there is no bookmark for that URL yet
            bookmark = next(
                (bookmark for bookmark in existing_bookmarks if bookmark.url == netscape_bookmark.href), None)
            if not bookmark:
                bookmark = Bookmark(owner=user)
                is_update = False
            else:
                is_update = True
            # Copy data from parsed bookmark
            _copy_bookmark_data(netscape_bookmark, bookmark)
            # Validate bookmark fields, exclude owner to prevent n+1 database query,
            # also there is no specific validation on owner
            bookmark.clean_fields(exclude=['owner'])
            # Schedule for update or insert
            if is_update:
                bookmarks_to_update.append(bookmark)
            else:
                bookmarks_to_create.append(bookmark)

            result.success = result.success + 1
        except:
            shortened_bookmark_tag_str = str(netscape_bookmark)[:100] + '...'
            logging.exception('Error importing bookmark: ' + shortened_bookmark_tag_str)
            result.failed = result.failed + 1

    # Bulk update bookmarks in DB
    Bookmark.objects.bulk_update(bookmarks_to_update,
                                 ['url', 'date_added', 'date_modified', 'unread', 'title', 'description', 'owner'])
    # Bulk insert new bookmarks into DB
    Bookmark.objects.bulk_create(bookmarks_to_create)

    # Bulk assign tags
    # In Django 3, bulk_create does not return the auto-generated IDs when bulk inserting,
    # so we have to reload the inserted bookmarks, and match them to the parsed bookmarks by URL
    existing_bookmarks = Bookmark.objects.filter(owner=user, url__in=batch_urls)

    BookmarkToTagRelationShip = Bookmark.tags.through
    relationships = []

    for netscape_bookmark in netscape_bookmarks:
        # Lookup bookmark by URL again
        bookmark = next(
            (bookmark for bookmark in existing_bookmarks if bookmark.url == netscape_bookmark.href), None)

        if not bookmark:
            # Something is wrong, we should have just created this bookmark
            shortened_bookmark_tag_str = str(netscape_bookmark)[:100] + '...'
            logging.warning(
                f'Failed to assign tags to the bookmark: {shortened_bookmark_tag_str}. Could not find bookmark by URL.')
            continue

        # Get tag models by string, schedule inserts for bookmark -> tag associations
        tag_names = parse_tag_string(netscape_bookmark.tag_string)
        tags = tag_cache.get_all(tag_names)
        for tag in tags:
            relationships.append(BookmarkToTagRelationShip(bookmark=bookmark, tag=tag))

    # Insert all bookmark -> tag associations at once, should ignore errors if association already exists
    BookmarkToTagRelationShip.objects.bulk_create(relationships, ignore_conflicts=True)


def _copy_bookmark_data(netscape_bookmark: NetscapeBookmark, bookmark: Bookmark):
    bookmark.url = netscape_bookmark.href
    if netscape_bookmark.date_added:
        bookmark.date_added = parse_timestamp(netscape_bookmark.date_added)
    else:
        bookmark.date_added = timezone.now()
    bookmark.date_modified = bookmark.date_added
    bookmark.unread = netscape_bookmark.to_read
    if netscape_bookmark.title:
        bookmark.title = netscape_bookmark.title
    if netscape_bookmark.description:
        bookmark.description = netscape_bookmark.description
