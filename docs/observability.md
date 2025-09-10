# Linkding Observability Guide

This guide covers the comprehensive observability features available in linkding, including OpenTelemetry instrumentation, health endpoints, alerting, and monitoring setup.

## Table of Contents

1. [Overview](#overview)
2. [OpenTelemetry Setup](#opentelemetry-setup)
3. [Health Endpoints](#health-endpoints)
4. [Alerting and Webhooks](#alerting-and-webhooks)
5. [Metrics Collection](#metrics-collection)
6. [Distributed Tracing](#distributed-tracing)
7. [Structured Logging](#structured-logging)
8. [Configuration Reference](#configuration-reference)
9. [Monitoring Integration](#monitoring-integration)
10. [Troubleshooting](#troubleshooting)

## Overview

Linkding comes with built-in comprehensive observability features that provide deep insights into application performance, health, and behavior. The observability stack includes:

- **OpenTelemetry Integration**: Full support for traces, metrics, and logs
- **Health Endpoints**: Kubernetes-ready health checks for orchestration
- **Alerting System**: Configurable webhooks and notifications
- **Business Metrics**: Application-specific metrics for bookmark operations
- **Error Tracking**: Comprehensive error monitoring and reporting

## OpenTelemetry Setup

### Automatic Instrumentation

Linkding automatically configures OpenTelemetry instrumentation on startup. The setup includes:

- **Django Framework**: HTTP request tracing, middleware instrumentation
- **Database Operations**: SQLite query tracing and performance metrics
- **HTTP Client**: Outbound request tracing (requests library)
- **Background Tasks**: Task execution tracing and metrics

### Configuration

OpenTelemetry is configured through environment variables:

```bash
# OTLP Collector endpoint (required)
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317

# Authentication token (optional)
OTEL_EXPORTER_OTLP_BEARER_TOKEN=your-token-here

# Service information
OTEL_SERVICE_NAME=linkding
OTEL_SERVICE_VERSION=1.0.0
```

### Manual Instrumentation

For custom instrumentation in your code:

```python
from otel import get_current_span_context, add_span_attributes, record_exception

# Add custom attributes to current span
add_span_attributes({
    "user.id": user.id,
    "bookmark.count": bookmark_count,
    "operation.type": "bulk_import"
})

# Record exceptions
try:
    # your code here
    pass
except Exception as e:
    record_exception(e)
    raise
```

## Health Endpoints

Linkding provides multiple health check endpoints suitable for different monitoring scenarios:

### Available Endpoints

#### `GET /health`
Basic liveness check - lightweight endpoint for quick health verification.

**Response:**
```json
{
  "status": "healthy",
  "timestamp": "2024-01-15T10:30:00Z",
  "service": "linkding",
  "version": "1.0.0"
}
```

#### `GET /health/live`
Kubernetes liveness probe - checks basic service responsiveness.

**Response:**
```json
{
  "status": "alive",
  "timestamp": "2024-01-15T10:30:00Z",
  "duration_ms": 12.5
}
```

#### `GET /health/ready`
Kubernetes readiness probe - comprehensive dependency checks.

**Response:**
```json
{
  "status": "healthy",
  "timestamp": "2024-01-15T10:30:00Z",
  "duration_ms": 45.2,
  "checks": [
    {
      "name": "database",
      "healthy": true,
      "message": "Database connection successful",
      "duration_ms": 15.3,
      "metadata": {
        "users": 10,
        "bookmarks": 150,
        "database_engine": "django.db.backends.sqlite3"
      }
    },
    {
      "name": "background_tasks",
      "healthy": true,
      "message": "Background task system operational",
      "duration_ms": 8.7,
      "metadata": {
        "enabled": true,
        "pending_tasks": 2,
        "failed_tasks": 0
      }
    }
  ]
}
```

#### `GET /health/metrics`
Detailed health metrics and system statistics.

### Kubernetes Configuration

For Kubernetes deployments, configure probes in your deployment:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: linkding
spec:
  template:
    spec:
      containers:
      - name: linkding
        livenessProbe:
          httpGet:
            path: /health/live
            port: 9090
          initialDelaySeconds: 30
          periodSeconds: 10
          timeoutSeconds: 5
          failureThreshold: 3
        
        readinessProbe:
          httpGet:
            path: /health/ready
            port: 9090
          initialDelaySeconds: 10
          periodSeconds: 5
          timeoutSeconds: 10
          failureThreshold: 3
```

## Alerting and Webhooks

The alerting system provides flexible notification capabilities for health issues, errors, and custom events.

### Configuration

Configure webhooks through environment variables:

```bash
# Webhook URLs (comma-separated for multiple)
LINKDING_WEBHOOK_URLS=https://alerts.example.com/webhook,https://slack.com/webhook/xyz

# Authentication
LINKDING_WEBHOOK_AUTH_TOKEN=your-webhook-token

# Custom headers (JSON format)
LINKDING_WEBHOOK_HEADERS='{"X-Custom-Header": "value"}'

# Retry configuration
LINKDING_WEBHOOK_TIMEOUT=10
LINKDING_WEBHOOK_RETRY_COUNT=3
LINKDING_WEBHOOK_RETRY_DELAY=1
```

### Alert Types

The system sends alerts for various events:

- **Health Check Failures**: Database connectivity, background tasks
- **High Error Rates**: When error thresholds are exceeded
- **System Resources**: Memory, disk usage warnings
- **Background Task Failures**: Failed import/export operations
- **Custom Alerts**: Application-specific notifications

### Webhook Payload

Webhooks receive JSON payloads with detailed alert information:

```json
{
  "alert": {
    "alert_id": "health_check_failed_1705320600",
    "alert_type": "health_check_failed",
    "severity": "error",
    "title": "Health Check Failed: database",
    "message": "Health check 'database' failed: Connection timeout",
    "timestamp": "2024-01-15T10:30:00Z",
    "metadata": {
      "check_name": "database",
      "error": "Connection timeout",
      "duration_ms": 5000
    },
    "tags": ["health", "monitoring"],
    "service": "linkding"
  },
  "service_metadata": {
    "service": "linkding",
    "version": "1.0.0",
    "timestamp": "2024-01-15T10:30:00Z"
  }
}
```

### Custom Alerts

Send custom alerts programmatically:

```python
from bookmarks.services.alerting import send_custom_alert, AlertSeverity

# Send a custom alert
send_custom_alert(
    title="Bulk Import Completed",
    message=f"Successfully imported {count} bookmarks",
    severity=AlertSeverity.INFO,
    metadata={"bookmark_count": count, "user_id": user.id},
    tags=["import", "success"]
)
```

## Metrics Collection

### Business Metrics

Linkding automatically collects business-specific metrics:

- `linkding_bookmark_operations_total`: Bookmark CRUD operations
- `linkding_import_operations_total`: Import operations count
- `linkding_export_operations_total`: Export operations count
- `linkding_search_operations_total`: Search operations count
- `linkding_import_duration_seconds`: Import operation duration
- `linkding_export_duration_seconds`: Export operation duration
- `linkding_bookmarks_total`: Current total bookmarks

### Health Metrics

- `linkding_health_checks_total`: Health check request count
- `linkding_health_check_duration_seconds`: Health check duration
- `linkding_dependency_checks_total`: Dependency check count

### Alerting Metrics

- `linkding_alerts_sent_total`: Alerts sent count by type and severity
- `linkding_webhook_requests_total`: Webhook request count and status
- `linkding_webhook_duration_seconds`: Webhook request duration

### Custom Metrics

Create custom metrics for your specific needs:

```python
from otel import get_business_meter

meter = get_business_meter()

# Create a counter
custom_counter = meter.create_counter(
    name="linkding_custom_operations_total",
    description="Custom operation count",
    unit="1"
)

# Record measurements
custom_counter.add(1, {"operation": "custom_action", "user_type": "admin"})
```

## Distributed Tracing

### Trace Context

OpenTelemetry automatically propagates trace context across:

- HTTP requests (Django middleware)
- Database operations (SQLite3 instrumentation)
- Background tasks (when properly configured)
- External HTTP calls (requests library)

### Trace Attributes

Standard attributes are automatically added to spans:

- HTTP request details (method, URL, status code)
- Database query information
- User context (when available)
- Error details and stack traces

### Custom Spans

Create custom spans for detailed tracing:

```python
from opentelemetry import trace

tracer = trace.get_tracer(__name__)

with tracer.start_as_current_span("bookmark.processing") as span:
    span.set_attribute("bookmark.id", bookmark.id)
    span.set_attribute("user.id", user.id)
    
    # Your code here
    process_bookmark(bookmark)
    
    span.set_attribute("processing.duration", duration)
```

## Structured Logging

### Log Correlation

All log entries automatically include trace and span IDs for correlation:

```json
{
  "timestamp": "2024-01-15T10:30:00Z",
  "level": "INFO",
  "message": "Bookmark created successfully",
  "trace_id": "1234567890abcdef",
  "span_id": "abcdef1234567890",
  "service": "linkding",
  "user_id": 123,
  "bookmark_id": 456
}
```

### Log Levels

Use appropriate log levels for different scenarios:

```python
import logging
logger = logging.getLogger(__name__)

# Info for normal operations
logger.info("User logged in", extra={"user_id": user.id})

# Warning for recoverable issues  
logger.warning("Import partially failed", extra={
    "imported": successful_count,
    "failed": failed_count
})

# Error for serious issues
logger.error("Database connection failed", extra={
    "error": str(e),
    "retry_count": retries
})
```

## Configuration Reference

### Environment Variables

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP collector endpoint | `http://localhost:4317` | Yes |
| `OTEL_EXPORTER_OTLP_BEARER_TOKEN` | Authentication token | None | No |
| `LINKDING_WEBHOOK_URLS` | Webhook URLs (comma-separated) | None | No |
| `LINKDING_WEBHOOK_AUTH_TOKEN` | Webhook auth token | None | No |
| `LINKDING_WEBHOOK_HEADERS` | Custom webhook headers (JSON) | `{}` | No |
| `LINKDING_WEBHOOK_TIMEOUT` | Webhook timeout seconds | `10` | No |
| `LINKDING_WEBHOOK_RETRY_COUNT` | Webhook retry attempts | `3` | No |
| `LINKDING_WEBHOOK_RETRY_DELAY` | Delay between retries | `1` | No |

### Django Settings

OpenTelemetry is initialized early in Django startup in `settings/base.py`:

```python
# Initialize OpenTelemetry instrumentation
from otel import setup_instrumentation
otel_logger, otel_tracer, otel_meter = setup_instrumentation(
    service_name="linkding",
    service_version="1.0.0"
)
```

## Monitoring Integration

### Prometheus

Linkding exports metrics in Prometheus format via the OTLP exporter. Configure your Prometheus to scrape the OTLP collector or use direct Prometheus exporters.

### Grafana Dashboards

Key metrics to monitor in Grafana:

1. **Application Health**
   - Health check success rates
   - Response times for health endpoints
   - Service availability

2. **Business Metrics**
   - Bookmark operations per second
   - Import/export success rates
   - User activity patterns

3. **System Performance**
   - HTTP request latency (P95, P99)
   - Database query performance
   - Error rates by endpoint

4. **Alerting Health**
   - Alert processing time
   - Webhook success rates
   - Failed notification counts

### Sample Grafana Queries

```promql
# Request rate
rate(django_http_requests_total[5m])

# Request latency P95
histogram_quantile(0.95, rate(django_http_request_duration_seconds_bucket[5m]))

# Health check success rate
rate(linkding_health_checks_total{status="healthy"}[5m]) / rate(linkding_health_checks_total[5m])

# Bookmark operations
rate(linkding_bookmark_operations_total[5m])
```

### Jaeger Tracing

Configure Jaeger for distributed tracing:

```yaml
# docker-compose.yml
version: '3'
services:
  jaeger:
    image: jaegertracing/all-in-one:latest
    ports:
      - "16686:16686"
      - "14268:14268"
    
  otel-collector:
    image: otel/opentelemetry-collector:latest
    command: ["--config=/etc/otel-collector-config.yaml"]
    volumes:
      - ./otel-collector-config.yaml:/etc/otel-collector-config.yaml
    ports:
      - "4317:4317"
```

## Troubleshooting

### Common Issues

#### 1. OpenTelemetry Not Working

**Symptoms**: No traces or metrics appearing in your collector

**Solutions**:
- Verify `OTEL_EXPORTER_OTLP_ENDPOINT` is correctly set
- Check network connectivity to the collector
- Validate collector configuration
- Check Django logs for OpenTelemetry initialization errors

#### 2. Health Checks Failing

**Symptoms**: Health endpoints returning 500 errors

**Solutions**:
- Check database connectivity
- Verify Django settings are correct
- Review application logs for specific error messages
- Test individual health check components

#### 3. Webhooks Not Receiving Alerts

**Symptoms**: No webhook notifications despite system issues

**Solutions**:
- Verify `LINKDING_WEBHOOK_URLS` configuration
- Check webhook endpoint accessibility
- Review webhook authentication settings
- Monitor webhook metrics for failure patterns

#### 4. High Memory Usage

**Symptoms**: Application consuming excessive memory

**Solutions**:
- Monitor span buffer sizes
- Adjust OTLP batch export settings
- Review metric cardinality
- Check for memory leaks in custom instrumentation

### Debug Mode

Enable debug logging for troubleshooting:

```python
import logging
logging.getLogger('otel').setLevel(logging.DEBUG)
logging.getLogger('bookmarks.services.alerting').setLevel(logging.DEBUG)
logging.getLogger('bookmarks.views.health').setLevel(logging.DEBUG)
```

### Health Check Testing

Test health endpoints manually:

```bash
# Basic health check
curl -v http://localhost:9090/health

# Comprehensive readiness check
curl -v http://localhost:9090/health/ready

# Liveness check
curl -v http://localhost:9090/health/live

# Metrics endpoint
curl -v http://localhost:9090/health/metrics
```

### Alert Testing

Test the alerting system:

```python
from bookmarks.services.alerting import send_custom_alert, AlertSeverity

# Send test alert
send_custom_alert(
    title="Test Alert",
    message="This is a test alert to verify webhook configuration",
    severity=AlertSeverity.INFO,
    metadata={"test": True}
)
```

## Best Practices

1. **Monitor Health Endpoints**: Set up monitoring for all health endpoints
2. **Configure Appropriate Timeouts**: Set reasonable timeouts for health checks
3. **Use Appropriate Alert Thresholds**: Avoid alert spam with proper thresholds
4. **Monitor Resource Usage**: Keep an eye on memory and CPU usage
5. **Regular Testing**: Test your observability setup regularly
6. **Document Custom Metrics**: Keep track of any custom metrics you add
7. **Review Alert Patterns**: Regularly review and tune alert configurations

For additional support or questions about observability features, consult the Django logs or check the OpenTelemetry documentation.