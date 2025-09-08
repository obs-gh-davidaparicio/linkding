"""
OpenTelemetry instrumentation setup for Django linkding application.

This module provides comprehensive OpenTelemetry instrumentation including:
- Distributed tracing with OTLP span export
- Metrics collection with OTLP metric export  
- Structured logging with OTLP log export
- Django-specific instrumentation
- SQLite3 and requests instrumentation
"""

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
from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def get_resource(service_name: str, service_version: str = "1.0.0") -> Resource:
    """
    Create OpenTelemetry resource with service attributes.
    
    Args:
        service_name: Logical service name for resource attributes.
        service_version: Service version for resource attributes.
        
    Returns:
        OpenTelemetry Resource instance with service attributes.
    """
    return Resource(attributes={
        SERVICE_NAME: service_name,
        SERVICE_VERSION: service_version,
    })


def setup_tracing(resource: Resource, otlp_endpoint: str, bearer_token: str = None) -> trace.Tracer:
    """
    Set up OpenTelemetry tracing with OTLP gRPC exporter.

    Args:
        resource: OpenTelemetry resource with service attributes.
        otlp_endpoint: Endpoint for the OTLP trace exporter.
        bearer_token: Optional bearer token for authentication.

    Returns:
        An OpenTelemetry Tracer instance.
    """
    trace_provider = TracerProvider(resource=resource)
    
    # Configure OTLP exporter with optional authentication
    exporter_kwargs = {"endpoint": otlp_endpoint}
    if bearer_token:
        exporter_kwargs["headers"] = {"authorization": f"Bearer {bearer_token}"}
    
    otlp_exporter = OTLPSpanExporter(**exporter_kwargs)
    otlp_processor = BatchSpanProcessor(otlp_exporter)
    trace_provider.add_span_processor(otlp_processor)
    trace.set_tracer_provider(trace_provider)
    return trace.get_tracer(__name__)


def setup_metrics(resource: Resource, otlp_endpoint: str, bearer_token: str = None) -> metrics.Meter:
    """
    Set up OpenTelemetry metrics with OTLP gRPC exporter.

    Args:
        resource: OpenTelemetry resource with service attributes.
        otlp_endpoint: Endpoint for the OTLP metric exporter.
        bearer_token: Optional bearer token for authentication.

    Returns:
        An OpenTelemetry Meter instance.
    """
    # Configure OTLP exporter with optional authentication
    exporter_kwargs = {"endpoint": otlp_endpoint}
    if bearer_token:
        exporter_kwargs["headers"] = {"authorization": f"Bearer {bearer_token}"}
    
    metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter(**exporter_kwargs))
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[metric_reader]))
    return metrics.get_meter(__name__)


def setup_logging(resource: Resource, otlp_endpoint: str, bearer_token: str = None) -> logging.Logger:
    """
    Set up OpenTelemetry logging with OTLP gRPC exporter.

    Args:
        resource: OpenTelemetry resource with service attributes.
        otlp_endpoint: Endpoint for the OTLP log exporter.
        bearer_token: Optional bearer token for authentication.

    Returns:
        A configured logger instance.
    """
    logger_provider = LoggerProvider(resource=resource)
    set_logger_provider(logger_provider)
    
    # Configure OTLP exporter with optional authentication
    exporter_kwargs = {"endpoint": otlp_endpoint}
    if bearer_token:
        exporter_kwargs["headers"] = {"authorization": f"Bearer {bearer_token}"}
    
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter(**exporter_kwargs)))

    # Add OpenTelemetry logging handler to root logger
    handler = LoggingHandler(level=logging.NOTSET, logger_provider=logger_provider)
    logging.getLogger().addHandler(handler)

    # Instrument logging to add trace context
    LoggingInstrumentor().instrument(set_logging_format=True)

    return logging.getLogger(__name__)


def setup_instrumentation(service_name: str = "linkding", service_version: str = "1.0.0") -> Tuple[logging.Logger, trace.Tracer, metrics.Meter]:
    """
    Set up comprehensive OpenTelemetry instrumentation for Django linkding application.

    Args:
        service_name: Logical service name for resource attributes.
        service_version: Service version for resource attributes.

    Returns:
        Tuple containing (logger, tracer, meter) instances.
    """
    # Get configuration from environment variables
    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    bearer_token = os.environ.get("OTEL_EXPORTER_OTLP_BEARER_TOKEN")
    
    # Create resource with service information
    resource = get_resource(service_name, service_version)
    
    # Set up tracing, metrics, and logging
    tracer = setup_tracing(resource, otlp_endpoint, bearer_token)
    meter = setup_metrics(resource, otlp_endpoint, bearer_token)
    logger = setup_logging(resource, otlp_endpoint, bearer_token)
    
    # Instrument Django framework
    DjangoInstrumentor().instrument()
    
    # Instrument SQLite3 database operations
    SQLite3Instrumentor().instrument()
    
    # Instrument HTTP requests made by the requests library
    RequestsInstrumentor().instrument()
    
    logger.info("OpenTelemetry instrumentation initialized", extra={
        "service_name": service_name,
        "service_version": service_version,
        "otlp_endpoint": otlp_endpoint,
        "instrumentation": ["django", "sqlite3", "requests", "logging"]
    })
    
    return logger, tracer, meter


def get_current_span_context():
    """
    Get the current span context for manual instrumentation.
    
    Returns:
        Current span context or None if no active span.
    """
    current_span = trace.get_current_span()
    if current_span and current_span.is_recording():
        return current_span.get_span_context()
    return None


def add_span_attributes(attributes: dict):
    """
    Add attributes to the current span if one is active.
    
    Args:
        attributes: Dictionary of attributes to add to the current span.
    """
    current_span = trace.get_current_span()
    if current_span and current_span.is_recording():
        for key, value in attributes.items():
            current_span.set_attribute(key, value)


def record_exception(exception: Exception):
    """
    Record an exception in the current span if one is active.
    
    Args:
        exception: Exception to record in the current span.
    """
    current_span = trace.get_current_span()
    if current_span and current_span.is_recording():
        current_span.record_exception(exception)
        current_span.set_status(trace.Status(trace.StatusCode.ERROR, str(exception)))
