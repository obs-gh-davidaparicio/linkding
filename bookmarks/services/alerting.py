"""
Alerting and webhook integration service for linkding.

This module provides comprehensive alerting capabilities including:
- Webhook notifications for system events
- Health check alerts and notifications
- Error threshold monitoring
- Integration with external monitoring systems
- OpenTelemetry instrumentation for alerting events

The alerting system is designed to be lightweight and configurable,
with minimal impact on application performance.
"""

import json
import logging
import time
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Callable
from urllib.parse import urlparse
from enum import Enum

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from opentelemetry import trace, metrics
from opentelemetry.trace import Status, StatusCode


# Get OpenTelemetry instances
tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)
logger = logging.getLogger(__name__)

# Alerting metrics
alert_counter = meter.create_counter(
    name="linkding_alerts_sent_total",
    description="Total number of alerts sent",
    unit="1"
)

webhook_counter = meter.create_counter(
    name="linkding_webhook_requests_total", 
    description="Total number of webhook requests",
    unit="1"
)

webhook_duration = meter.create_histogram(
    name="linkding_webhook_duration_seconds",
    description="Duration of webhook requests",
    unit="s"
)

alert_processing_duration = meter.create_histogram(
    name="linkding_alert_processing_duration_seconds",
    description="Time taken to process alerts", 
    unit="s"
)


class AlertSeverity(Enum):
    """Alert severity levels."""
    INFO = "info"
    WARNING = "warning" 
    ERROR = "error"
    CRITICAL = "critical"


class AlertType(Enum):
    """Types of alerts that can be sent."""
    HEALTH_CHECK_FAILED = "health_check_failed"
    DATABASE_ERROR = "database_error"
    HIGH_ERROR_RATE = "high_error_rate"
    SYSTEM_RESOURCE = "system_resource"
    BACKGROUND_TASK_FAILED = "background_task_failed"
    USER_ACTION = "user_action"
    CUSTOM = "custom"


class Alert:
    """Represents an alert to be sent."""
    
    def __init__(self, 
                 alert_type: AlertType,
                 severity: AlertSeverity,
                 title: str,
                 message: str,
                 metadata: Dict[str, Any] = None,
                 tags: List[str] = None):
        self.alert_type = alert_type
        self.severity = severity
        self.title = title
        self.message = message
        self.metadata = metadata or {}
        self.tags = tags or []
        self.timestamp = timezone.now()
        self.alert_id = f"{alert_type.value}_{int(self.timestamp.timestamp())}"
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert alert to dictionary for serialization."""
        return {
            "alert_id": self.alert_id,
            "alert_type": self.alert_type.value,
            "severity": self.severity.value,
            "title": self.title,
            "message": self.message,
            "timestamp": self.timestamp.isoformat(),
            "metadata": self.metadata,
            "tags": self.tags,
            "service": "linkding"
        }


class WebhookConfig:
    """Configuration for webhook endpoints."""
    
    def __init__(self, 
                 url: str,
                 headers: Dict[str, str] = None,
                 timeout: int = 10,
                 retry_count: int = 3,
                 retry_delay: int = 1,
                 enabled: bool = True,
                 filter_severities: List[AlertSeverity] = None,
                 filter_types: List[AlertType] = None):
        self.url = url
        self.headers = headers or {}
        self.timeout = timeout
        self.retry_count = retry_count
        self.retry_delay = retry_delay
        self.enabled = enabled
        self.filter_severities = filter_severities or []
        self.filter_types = filter_types or []
    
    def should_send_alert(self, alert: Alert) -> bool:
        """Determine if this webhook should receive the alert."""
        if not self.enabled:
            return False
        
        if self.filter_severities and alert.severity not in self.filter_severities:
            return False
        
        if self.filter_types and alert.alert_type not in self.filter_types:
            return False
        
        return True


class AlertingService:
    """Main alerting service for managing notifications and webhooks."""
    
    def __init__(self):
        self.webhooks: List[WebhookConfig] = []
        self.alert_handlers: Dict[AlertType, List[Callable]] = {}
        self._load_configuration()
    
    def _load_configuration(self):
        """Load alerting configuration from Django settings."""
        # Load webhook configurations from environment/settings
        webhook_urls = self._get_webhook_urls()
        for url in webhook_urls:
            webhook_config = WebhookConfig(
                url=url,
                headers=self._get_webhook_headers(url),
                timeout=int(self._get_setting('LINKDING_WEBHOOK_TIMEOUT', 10)),
                retry_count=int(self._get_setting('LINKDING_WEBHOOK_RETRY_COUNT', 3)),
                retry_delay=int(self._get_setting('LINKDING_WEBHOOK_RETRY_DELAY', 1))
            )
            self.webhooks.append(webhook_config)
    
    def _get_setting(self, key: str, default: Any = None) -> Any:
        """Get setting from environment or Django settings."""
        import os
        return os.getenv(key, getattr(settings, key.replace('LINKDING_', ''), default))
    
    def _get_webhook_urls(self) -> List[str]:
        """Get webhook URLs from configuration."""
        urls_str = self._get_setting('LINKDING_WEBHOOK_URLS', '')
        if not urls_str:
            return []
        return [url.strip() for url in urls_str.split(',') if url.strip()]
    
    def _get_webhook_headers(self, url: str) -> Dict[str, str]:
        """Get headers for a specific webhook URL."""
        headers = {'Content-Type': 'application/json'}
        
        # Check for authentication tokens
        auth_token = self._get_setting('LINKDING_WEBHOOK_AUTH_TOKEN')
        if auth_token:
            headers['Authorization'] = f'Bearer {auth_token}'
        
        # Check for custom headers
        custom_headers = self._get_setting('LINKDING_WEBHOOK_HEADERS', '{}')
        try:
            headers.update(json.loads(custom_headers))
        except (json.JSONDecodeError, TypeError):
            logger.warning("Invalid webhook headers configuration", extra={
                "custom_headers": custom_headers
            })
        
        return headers
    
    def register_handler(self, alert_type: AlertType, handler: Callable[[Alert], None]):
        """Register a custom alert handler."""
        if alert_type not in self.alert_handlers:
            self.alert_handlers[alert_type] = []
        self.alert_handlers[alert_type].append(handler)
    
    def send_alert(self, alert: Alert) -> bool:
        """
        Send an alert through all configured channels.
        
        Args:
            alert: Alert to send
            
        Returns:
            bool: True if alert was sent successfully through at least one channel
        """
        start_time = time.time()
        
        with tracer.start_as_current_span("alert.send") as span:
            try:
                span.set_attribute("alert.type", alert.alert_type.value)
                span.set_attribute("alert.severity", alert.severity.value)
                span.set_attribute("alert.id", alert.alert_id)
                
                success_count = 0
                total_attempts = 0
                
                # Send to webhooks
                for webhook in self.webhooks:
                    if webhook.should_send_alert(alert):
                        total_attempts += 1
                        if self._send_webhook(webhook, alert):
                            success_count += 1
                
                # Execute custom handlers
                handlers = self.alert_handlers.get(alert.alert_type, [])
                for handler in handlers:
                    try:
                        handler(alert)
                        success_count += 1
                    except Exception as e:
                        logger.error("Alert handler failed", extra={
                            "handler": str(handler),
                            "alert_id": alert.alert_id,
                            "error": str(e)
                        })
                
                # Record metrics
                duration = time.time() - start_time
                alert_processing_duration.record(duration, {
                    "alert_type": alert.alert_type.value,
                    "severity": alert.severity.value
                })
                
                alert_counter.add(1, {
                    "alert_type": alert.alert_type.value,
                    "severity": alert.severity.value,
                    "status": "success" if success_count > 0 else "failed"
                })
                
                # Log result
                if success_count > 0:
                    logger.info("Alert sent successfully", extra={
                        "alert_id": alert.alert_id,
                        "alert_type": alert.alert_type.value,
                        "success_count": success_count,
                        "total_attempts": total_attempts,
                        "duration_ms": duration * 1000
                    })
                    span.set_status(Status(StatusCode.OK))
                    return True
                else:
                    logger.warning("Failed to send alert", extra={
                        "alert_id": alert.alert_id,
                        "alert_type": alert.alert_type.value,
                        "total_attempts": total_attempts,
                        "duration_ms": duration * 1000
                    })
                    span.set_status(Status(StatusCode.ERROR, "No successful alert deliveries"))
                    return False
                
            except Exception as e:
                duration = time.time() - start_time
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, str(e)))
                
                logger.error("Alert processing failed", extra={
                    "alert_id": alert.alert_id,
                    "error": str(e),
                    "duration_ms": duration * 1000
                })
                
                return False
    
    def _send_webhook(self, webhook: WebhookConfig, alert: Alert) -> bool:
        """Send alert to a specific webhook endpoint."""
        start_time = time.time()
        
        with tracer.start_as_current_span("webhook.send") as span:
            span.set_attribute("webhook.url", webhook.url)
            span.set_attribute("alert.id", alert.alert_id)
            
            payload = {
                "alert": alert.to_dict(),
                "service_metadata": {
                    "service": "linkding",
                    "version": "1.0.0", 
                    "timestamp": timezone.now().isoformat()
                }
            }
            
            for attempt in range(webhook.retry_count):
                try:
                    response = requests.post(
                        webhook.url,
                        json=payload,
                        headers=webhook.headers,
                        timeout=webhook.timeout
                    )
                    
                    duration = time.time() - start_time
                    
                    webhook_duration.record(duration, {
                        "webhook_host": urlparse(webhook.url).netloc,
                        "status_code": str(response.status_code),
                        "attempt": str(attempt + 1)
                    })
                    
                    webhook_counter.add(1, {
                        "webhook_host": urlparse(webhook.url).netloc,
                        "status_code": str(response.status_code),
                        "attempt": str(attempt + 1),
                        "alert_type": alert.alert_type.value
                    })
                    
                    if response.status_code < 400:
                        span.set_attribute("webhook.status_code", response.status_code)
                        span.set_attribute("webhook.attempts", attempt + 1)
                        span.set_status(Status(StatusCode.OK))
                        
                        logger.debug("Webhook sent successfully", extra={
                            "webhook_url": webhook.url,
                            "alert_id": alert.alert_id,
                            "status_code": response.status_code,
                            "attempt": attempt + 1,
                            "duration_ms": duration * 1000
                        })
                        
                        return True
                    else:
                        logger.warning("Webhook returned error status", extra={
                            "webhook_url": webhook.url,
                            "alert_id": alert.alert_id,
                            "status_code": response.status_code,
                            "response_text": response.text[:200],
                            "attempt": attempt + 1
                        })
                
                except requests.exceptions.RequestException as e:
                    duration = time.time() - start_time
                    
                    webhook_counter.add(1, {
                        "webhook_host": urlparse(webhook.url).netloc,
                        "status_code": "error",
                        "attempt": str(attempt + 1),
                        "alert_type": alert.alert_type.value
                    })
                    
                    logger.warning("Webhook request failed", extra={
                        "webhook_url": webhook.url,
                        "alert_id": alert.alert_id,
                        "error": str(e),
                        "attempt": attempt + 1,
                        "duration_ms": duration * 1000
                    })
                
                # Wait before retry (except on last attempt)
                if attempt < webhook.retry_count - 1:
                    time.sleep(webhook.retry_delay)
            
            # All attempts failed
            span.set_status(Status(StatusCode.ERROR, "All webhook attempts failed"))
            logger.error("Webhook failed after all attempts", extra={
                "webhook_url": webhook.url,
                "alert_id": alert.alert_id,
                "total_attempts": webhook.retry_count
            })
            
            return False
    
    def send_health_check_alert(self, check_name: str, error_message: str, metadata: Dict[str, Any] = None):
        """Send a health check failure alert."""
        alert = Alert(
            alert_type=AlertType.HEALTH_CHECK_FAILED,
            severity=AlertSeverity.ERROR,
            title=f"Health Check Failed: {check_name}",
            message=f"Health check '{check_name}' failed: {error_message}",
            metadata={
                "check_name": check_name,
                "error": error_message,
                **(metadata or {})
            },
            tags=["health", "monitoring"]
        )
        return self.send_alert(alert)
    
    def send_error_threshold_alert(self, error_count: int, threshold: int, time_window: str):
        """Send an alert when error threshold is exceeded."""
        alert = Alert(
            alert_type=AlertType.HIGH_ERROR_RATE,
            severity=AlertSeverity.WARNING,
            title="High Error Rate Detected", 
            message=f"Error count ({error_count}) exceeded threshold ({threshold}) in {time_window}",
            metadata={
                "error_count": error_count,
                "threshold": threshold,
                "time_window": time_window
            },
            tags=["errors", "monitoring"]
        )
        return self.send_alert(alert)
    
    def send_custom_alert(self, 
                         title: str,
                         message: str,
                         severity: AlertSeverity = AlertSeverity.INFO,
                         metadata: Dict[str, Any] = None,
                         tags: List[str] = None):
        """Send a custom alert."""
        alert = Alert(
            alert_type=AlertType.CUSTOM,
            severity=severity,
            title=title,
            message=message,
            metadata=metadata,
            tags=tags
        )
        return self.send_alert(alert)


# Global alerting service instance
_alerting_service = None

def get_alerting_service() -> AlertingService:
    """Get the global alerting service instance."""
    global _alerting_service
    if _alerting_service is None:
        _alerting_service = AlertingService()
    return _alerting_service


# Convenience functions for common alert types
def send_health_alert(check_name: str, error_message: str, metadata: Dict[str, Any] = None) -> bool:
    """Send a health check alert."""
    return get_alerting_service().send_health_check_alert(check_name, error_message, metadata)


def send_error_alert(error_count: int, threshold: int, time_window: str) -> bool:
    """Send an error threshold alert."""
    return get_alerting_service().send_error_threshold_alert(error_count, threshold, time_window)


def send_custom_alert(title: str, message: str, severity: AlertSeverity = AlertSeverity.INFO,
                     metadata: Dict[str, Any] = None, tags: List[str] = None) -> bool:
    """Send a custom alert."""
    return get_alerting_service().send_custom_alert(title, message, severity, metadata, tags)


class AlertThrottler:
    """Throttle duplicate alerts to prevent spam."""
    
    def __init__(self, cache_ttl: int = 300):  # 5 minutes default
        self.cache_ttl = cache_ttl
    
    def should_send_alert(self, alert: Alert) -> bool:
        """Check if alert should be sent or is being throttled."""
        # Create throttle key based on alert type and key attributes
        throttle_key = f"alert_throttle_{alert.alert_type.value}_{hash(alert.title)}"
        
        # Check if we've seen this alert recently
        if cache.get(throttle_key):
            logger.debug("Alert throttled", extra={
                "alert_id": alert.alert_id,
                "throttle_key": throttle_key
            })
            return False
        
        # Set throttle cache entry
        cache.set(throttle_key, True, self.cache_ttl)
        return True


# Global throttler instance
_alert_throttler = AlertThrottler()

def get_alert_throttler() -> AlertThrottler:
    """Get the global alert throttler instance."""
    return _alert_throttler