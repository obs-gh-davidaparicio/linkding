from typing import List

from opentelemetry import trace
from otel import get_business_meter
from bookmarks.models import Bookmark

tracer = trace.get_tracer(__name__)
meter = get_business_meter()

# Business metrics
export_operations_counter = meter.create_counter(
    name="linkding_export_operations_total",
    description="Total number of export operations",
    unit="1"
)

BookmarkDocument = List[str]


def export_netscape_html(bookmarks: List[Bookmark]):
    with tracer.start_as_current_span("export_netscape_html") as span:
        span.set_attribute("export.bookmark_count", len(bookmarks))

        doc = []
        append_header(doc)
        append_list_start(doc)
        [append_bookmark(doc, bookmark) for bookmark in bookmarks]
        append_list_end(doc)

        result = '\n\r'.join(doc)
        span.set_attribute("export.html_size", len(result))

        # Record business metric
        export_operations_counter.add(1, {"bookmark_count": str(len(bookmarks))})

        return result


def append_header(doc: BookmarkDocument):
    doc.append('<!DOCTYPE NETSCAPE-Bookmark-file-1>')
    doc.append('<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">')
    doc.append('<TITLE>Bookmarks</TITLE>')
    doc.append('<H1>Bookmarks</H1>')


def append_list_start(doc: BookmarkDocument):
    doc.append('<DL><p>')


def append_bookmark(doc: BookmarkDocument, bookmark: Bookmark):
    url = bookmark.url
    title = bookmark.resolved_title
    desc = bookmark.resolved_description
    tags = ','.join(bookmark.tag_names)
    toread = '1' if bookmark.unread else '0'
    added = int(bookmark.date_added.timestamp())

    doc.append(f'<DT><A HREF="{url}" ADD_DATE="{added}" PRIVATE="0" TOREAD="{toread}" TAGS="{tags}">{title}</A>')

    if desc:
        doc.append(f'<DD>{desc}')


def append_list_end(doc: BookmarkDocument):
    doc.append('</DL><p>')
