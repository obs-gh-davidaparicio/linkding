from typing import Optional

from django.contrib.auth.models import User
from django.db.models import Q, QuerySet
from opentelemetry import trace
from otel import get_business_meter

from bookmarks.models import Bookmark, Tag
from bookmarks.utils import unique

tracer = trace.get_tracer(__name__)
meter = get_business_meter()

# Business metrics
search_operations_counter = meter.create_counter(
    name="linkding_search_operations_total",
    description="Total number of search operations",
    unit="1"
)


def query_bookmarks(user: User, query_string: str) -> QuerySet:
    with tracer.start_as_current_span("query_bookmarks") as span:
        span.set_attribute("user.id", user.id)
        span.set_attribute("user.username", user.username)
        span.set_attribute("search.query", query_string or "")
        span.set_attribute("search.type", "active")

        result = _base_bookmarks_query(user, query_string).filter(is_archived=False)

        # Record business metric
        search_operations_counter.add(1, {
            "user_id": str(user.id),
            "search_type": "active",
            "has_query": str(bool(query_string))
        })

        return result


def query_archived_bookmarks(user: User, query_string: str) -> QuerySet:
    with tracer.start_as_current_span("query_archived_bookmarks") as span:
        span.set_attribute("user.id", user.id)
        span.set_attribute("user.username", user.username)
        span.set_attribute("search.query", query_string or "")
        span.set_attribute("search.type", "archived")

        result = _base_bookmarks_query(user, query_string).filter(is_archived=True)

        # Record business metric
        search_operations_counter.add(1, {
            "user_id": str(user.id),
            "search_type": "archived",
            "has_query": str(bool(query_string))
        })

        return result


def query_shared_bookmarks(user: Optional[User], query_string: str) -> QuerySet:
    with tracer.start_as_current_span("query_shared_bookmarks") as span:
        if user:
            span.set_attribute("user.id", user.id)
            span.set_attribute("user.username", user.username)
        span.set_attribute("search.query", query_string or "")
        span.set_attribute("search.type", "shared")

        result = _base_bookmarks_query(user, query_string) \
            .filter(shared=True) \
            .filter(owner__profile__enable_sharing=True)

        # Record business metric
        search_operations_counter.add(1, {
            "user_id": str(user.id) if user else "anonymous",
            "search_type": "shared",
            "has_query": str(bool(query_string))
        })

        return result


def _base_bookmarks_query(user: Optional[User], query_string: str) -> QuerySet:
    query_set = Bookmark.objects

    # Filter for user
    if user:
        query_set = query_set.filter(owner=user)

    # Split query into search terms and tags
    query = parse_query_string(query_string)

    # Filter for search terms and tags
    for term in query['search_terms']:
        query_set = query_set.filter(
            Q(title__contains=term)
            | Q(description__contains=term)
            | Q(website_title__contains=term)
            | Q(website_description__contains=term)
            | Q(url__contains=term)
        )

    for tag_name in query['tag_names']:
        query_set = query_set.filter(
            tags__name__iexact=tag_name
        )

    # Untagged bookmarks
    if query['untagged']:
        query_set = query_set.filter(
            tags=None
        )
    # Unread bookmarks
    if query['unread']:
        query_set = query_set.filter(
            unread=True
        )

    # Sort by date added
    query_set = query_set.order_by('-date_added')

    return query_set


def query_bookmark_tags(user: User, query_string: str) -> QuerySet:
    bookmarks_query = query_bookmarks(user, query_string)

    query_set = Tag.objects.filter(bookmark__in=bookmarks_query)

    return query_set.distinct()


def query_archived_bookmark_tags(user: User, query_string: str) -> QuerySet:
    bookmarks_query = query_archived_bookmarks(user, query_string)

    query_set = Tag.objects.filter(bookmark__in=bookmarks_query)

    return query_set.distinct()


def query_shared_bookmark_tags(user: Optional[User], query_string: str) -> QuerySet:
    bookmarks_query = query_shared_bookmarks(user, query_string)

    query_set = Tag.objects.filter(bookmark__in=bookmarks_query)

    return query_set.distinct()


def query_shared_bookmark_users(query_string: str) -> QuerySet:
    bookmarks_query = query_shared_bookmarks(None, query_string)

    query_set = User.objects.filter(bookmark__in=bookmarks_query)

    return query_set.distinct()


def get_user_tags(user: User):
    return Tag.objects.filter(owner=user).all()


def parse_query_string(query_string):
    # Sanitize query params
    if not query_string:
        query_string = ''

    # Split query into search terms and tags
    keywords = query_string.strip().split(' ')
    keywords = [word for word in keywords if word]

    search_terms = [word for word in keywords if word[0] != '#' and word[0] != '!']
    tag_names = [word[1:] for word in keywords if word[0] == '#']
    tag_names = unique(tag_names, str.lower)

    # Special search commands
    untagged = '!untagged' in keywords
    unread = '!unread' in keywords

    return {
        'search_terms': search_terms,
        'tag_names': tag_names,
        'untagged': untagged,
        'unread': unread,
    }
