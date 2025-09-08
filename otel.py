import logging
import os
from typing import Tuple

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.django import DjangoInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.instrumentation.sqlite3 import SQLite3Instrumentor
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def setup_tracing(resource: Resource, otlp_endpoint: str, bearer_token: str = None) -> trace.Tracer:
    """
    Set up OpenTelemetry tracing.

    Args:
        resource: OpenTelemetry resource with service attributes.
        otlp_endpoint: Endpoint for the OTLP trace exporter.
        bearer_token: Optional bearer token for authentication.

    Returns:
        An OpenTelemetry Tracer instance.
    """
    trace_provider = TracerProvider(resource=resource)
    
    # Configure headers for authentication if bearer token is provided
    headers = {}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    
    otlp_exporter = OTLPSpanExporter(endpoint=otlp_endpoint, headers=headers)
    otlp_processor = BatchSpanProcessor(otlp_exporter)
    trace_provider.add_span_processor(otlp_processor)
    trace.set_tracer_provider(trace_provider)
    return trace.get_tracer(__name__)


def setup_metrics(resource: Resource, otlp_endpoint: str, bearer_token: str = None) -> metrics.Meter:
    """
    Set up OpenTelemetry metrics.

    Args:
        resource: OpenTelemetry resource with service attributes.
        otlp_endpoint: Endpoint for the OTLP metric exporter.
        bearer_token: Optional bearer token for authentication.

    Returns:
        An OpenTelemetry Meter instance.
    """
    # Configure headers for authentication if bearer token is provided
    headers = {}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    
    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=otlp_endpoint, headers=headers),
        export_interval_millis=5000  # Export metrics every 5 seconds
    )
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[metric_reader]))
    return metrics.get_meter(__name__)


def setup_logging(resource: Resource, otlp_endpoint: str, bearer_token: str = None) -> logging.Logger:
    """
    Set up OpenTelemetry logging.

    Args:
        resource: OpenTelemetry resource with service attributes.
        otlp_endpoint: Endpoint for the OTLP log exporter.
        bearer_token: Optional bearer token for authentication.

    Returns:
        A configured root logger.
    """
    logger_provider = LoggerProvider(resource=resource)
    set_logger_provider(logger_provider)
    
    # Configure headers for authentication if bearer token is provided
    headers = {}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=otlp_endpoint, headers=headers))
    )

    handler = LoggingHandler(level=logging.NOTSET, logger_provider=logger_provider)
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)

    LoggingInstrumentor().instrument(set_logging_format=True)

    return logging.getLogger(__name__)


def setup_instrumentation(service_name: str = "linkding") -> Tuple[logging.Logger, trace.Tracer, metrics.Meter]:
    """
    Instrument a Django application with OpenTelemetry.

    Args:
        service_name: Logical service name for resource attributes.

    Returns:
        Tuple containing (logger, tracer, meter) instances.
    """
    # Instrument Django and related libraries
    DjangoInstrumentor().instrument()
    RequestsInstrumentor().instrument()
    SQLite3Instrumentor().instrument()

    resource = Resource(attributes={SERVICE_NAME: service_name})
    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    bearer_token = os.environ.get("OTEL_EXPORTER_OTLP_BEARER_TOKEN")

    tracer = setup_tracing(resource, otlp_endpoint, bearer_token)
    meter = setup_metrics(resource, otlp_endpoint, bearer_token)
    logger = setup_logging(resource, otlp_endpoint, bearer_token)

    return logger, tracer, meter


# Global instances for easy access throughout the application
logger = None
tracer = None
meter = None


def initialize_otel():
    """
    Initialize OpenTelemetry instrumentation.
    This should be called early in the Django application startup.
    """
    global logger, tracer, meter
    if logger is None:  # Only initialize once
        otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        if otlp_endpoint and otlp_endpoint.strip():
            logger, tracer, meter = setup_instrumentation()
            logging.getLogger(__name__).info("OpenTelemetry instrumentation initialized")
        else:
            # No OTLP endpoint configured, skip OpenTelemetry initialization
            logger = logging.getLogger(__name__)
            tracer = None
            meter = None
            logging.getLogger(__name__).info("OpenTelemetry disabled - no OTLP endpoint configured")


def get_logger() -> logging.Logger:
    """Get the OpenTelemetry logger instance."""
    if logger is None:
        initialize_otel()
    return logger


def get_tracer() -> trace.Tracer:
    """Get the OpenTelemetry tracer instance."""
    if tracer is None:
        initialize_otel()
    return tracer


def get_meter() -> metrics.Meter:
    """Get the OpenTelemetry meter instance."""
    if meter is None:
        initialize_otel()
    return meter
