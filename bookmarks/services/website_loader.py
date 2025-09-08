import logging
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup
from charset_normalizer import from_bytes
from opentelemetry import trace

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


@dataclass
class WebsiteMetadata:
    url: str
    title: str
    description: str

    def to_dict(self):
        return {
            'url': self.url,
            'title': self.title,
            'description': self.description,
        }


def load_website_metadata(url: str):
    with tracer.start_as_current_span("load_website_metadata") as span:
        span.set_attribute("website.url", url)

        title = None
        description = None
        try:
            page_text = load_page(url)
            soup = BeautifulSoup(page_text, 'html.parser')

            title = soup.title.string if soup.title is not None else None
            description_tag = soup.find('meta', attrs={'name': 'description'})
            description = description_tag['content'] if description_tag is not None else None

            span.set_attribute("website.title", title or "")
            span.set_attribute("website.description", description or "")

        except Exception as e:
            span.record_exception(e)
            span.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))
            logger.error(f"Failed to load website metadata for {url}", exc_info=e)
        finally:
            return WebsiteMetadata(url=url, title=title, description=description)


CHUNK_SIZE = 50 * 1024
MAX_CONTENT_LIMIT = 5000 * 1024


def load_page(url: str):
    with tracer.start_as_current_span("load_page") as span:
        span.set_attribute("http.url", url)
        span.set_attribute("http.method", "GET")

        headers = fake_request_headers()
        size = 0
        content = None
        # Use with to ensure request gets closed even if it's only read partially
        with requests.get(url, timeout=10, headers=headers, stream=True) as r:
            # Set attributes safely (handle mock objects in tests)
            if hasattr(r, 'status_code'):
                span.set_attribute("http.status_code", r.status_code)
            if hasattr(r, 'headers'):
                span.set_attribute("http.response.content_type", r.headers.get('content-type', ''))

            for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                size += len(chunk)
                if content is None:
                    content = chunk
                else:
                    content = content + chunk

                # Stop reading if we have parsed end of head tag
                if '</head>'.encode('utf-8') in content:
                    logger.debug(f'Found closing head tag after {size} bytes')
                    break
                # Stop reading if we exceed limit
                if size > MAX_CONTENT_LIMIT:
                    logger.debug(f'Cancel reading document after {size} bytes')
                    break

        span.set_attribute("http.response.body_size", size)

        # Use charset_normalizer to determine encoding that best matches the response content
        # Several sites seem to specify the response encoding incorrectly, so we ignore it and use custom logic instead
        # This is different from Response.text which does respect the encoding specified in the response first,
        # before trying to determine one
        results = from_bytes(content or '')
        return str(results.best())


DEFAULT_USER_AGENT = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/101.0.0.0 Safari/537.36'


def fake_request_headers():
    return {
        "Accept": "text/html,application/xhtml+xml,application/xml",
        "Accept-Encoding": "gzip, deflate",
        "Dnt": "1",
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": DEFAULT_USER_AGENT,
    }
