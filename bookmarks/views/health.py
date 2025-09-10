"""
Health check endpoints for monitoring and orchestration readiness.

This module provides comprehensive health check endpoints for:
- Basic liveness checks
- Database connectivity  
- Service dependency validation
- Orchestration (Kubernetes) readiness/liveness probes

The endpoints follow standard health check patterns and include
proper OpenTelemetry instrumentation for observability.
"""

import json
import logging
import time
from datetime import datetime, timedelta
from typing import Dict, Any, List

from django.conf import settings
from django.core.cache import cache
from django.db import connections, DatabaseError
from django.http import JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from opentelemetry import trace, metrics
from opentelemetry.trace import Status, StatusCode

from bookmarks.models import Bookmark, User
from bookmarks.services import tasks
from bookmarks.services.alerting import send_health_alert, get_alert_throttler

# Get OpenTelemetry instances
tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)
logger = logging.getLogger(__name__)

# Health check metrics
health_check_counter = meter.create_counter(
    name="linkding_health_checks_total",
    description="Total number of health check requests",
    unit="1"
)

health_check_duration = meter.create_histogram(
    name="linkding_health_check_duration_seconds", 
    description="Duration of health check operations",
    unit="s"
)

dependency_check_counter = meter.create_counter(
    name="linkding_dependency_checks_total",
    description="Total number of dependency checks",
    unit="1"
)

# Cache keys for health check results
HEALTH_CACHE_KEY = "health_check_result"
HEALTH_CACHE_TTL = 30  # 30 seconds cache


class HealthCheckResult:
    """Represents the result of a health check operation."""
    
    def __init__(self, name: str, healthy: bool, message: str = "", 
                 duration_ms: float = 0, metadata: Dict[str, Any] = None):
        self.name = name
        self.healthy = healthy
        self.message = message
        self.duration_ms = duration_ms
        self.metadata = metadata or {}
        self.timestamp = timezone.now()
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert result to dictionary for JSON serialization."""
        return {
            "name": self.name,
            "healthy": self.healthy,
            "message": self.message,
            "duration_ms": round(self.duration_ms, 2),
            "timestamp": self.timestamp.isoformat(),
            "metadata": self.metadata
        }


def check_database_connectivity() -> HealthCheckResult:
    """
    Check database connectivity and basic query operations.
    
    Returns:
        HealthCheckResult indicating database health status.
    """
    start_time = time.time()
    
    try:
        # Test database connection
        db_conn = connections['default']
        db_conn.ensure_connection()
        
        # Test basic query - count users (lightweight operation)
        user_count = User.objects.count()
        bookmark_count = Bookmark.objects.count()
        
        duration_ms = (time.time() - start_time) * 1000
        
        return HealthCheckResult(
            name="database",
            healthy=True,
            message="Database connection successful",
            duration_ms=duration_ms,
            metadata={
                "users": user_count,
                "bookmarks": bookmark_count,
                "database_engine": settings.DATABASES['default']['ENGINE']
            }
        )
        
    except DatabaseError as e:
        duration_ms = (time.time() - start_time) * 1000
        logger.error("Database health check failed", extra={
            "error": str(e),
            "duration_ms": duration_ms
        })
        
        # Send alert if throttler allows it
        throttler = get_alert_throttler()
        from bookmarks.services.alerting import Alert, AlertType, AlertSeverity
        alert = Alert(
            alert_type=AlertType.DATABASE_ERROR,
            severity=AlertSeverity.ERROR,
            title="Database Health Check Failed",
            message=f"Database connection failed: {str(e)}"
        )
        if throttler.should_send_alert(alert):
            send_health_alert("database", str(e), {"duration_ms": duration_ms})
        
        return HealthCheckResult(
            name="database",
            healthy=False,
            message=f"Database connection failed: {str(e)}",
            duration_ms=duration_ms
        )
    
    except Exception as e:
        duration_ms = (time.time() - start_time) * 1000
        logger.error("Unexpected error in database health check", extra={
            "error": str(e),
            "duration_ms": duration_ms
        })
        
        return HealthCheckResult(
            name="database",
            healthy=False, 
            message=f"Unexpected database error: {str(e)}",
            duration_ms=duration_ms
        )


def check_background_tasks() -> HealthCheckResult:
    """
    Check background task system health.
    
    Returns:
        HealthCheckResult indicating background task system status.
    """
    start_time = time.time()
    
    try:
        if getattr(settings, 'LD_DISABLE_BACKGROUND_TASKS', False):
            return HealthCheckResult(
                name="background_tasks",
                healthy=True,
                message="Background tasks disabled by configuration",
                duration_ms=(time.time() - start_time) * 1000,
                metadata={"enabled": False}
            )
        
        # Check if we can import and access task functions
        from background_task.models import Task
        pending_tasks = Task.objects.filter(failed_at__isnull=True).count()
        failed_tasks = Task.objects.filter(failed_at__isnull=False).count()
        
        duration_ms = (time.time() - start_time) * 1000
        
        # Consider unhealthy if too many failed tasks
        healthy = failed_tasks < 10  # Threshold for failed tasks
        message = "Background task system operational" if healthy else f"High number of failed tasks: {failed_tasks}"
        
        return HealthCheckResult(
            name="background_tasks",
            healthy=healthy,
            message=message,
            duration_ms=duration_ms,
            metadata={
                "enabled": True,
                "pending_tasks": pending_tasks,
                "failed_tasks": failed_tasks
            }
        )
        
    except Exception as e:
        duration_ms = (time.time() - start_time) * 1000
        logger.warning("Background task health check failed", extra={
            "error": str(e),
            "duration_ms": duration_ms
        })
        
        return HealthCheckResult(
            name="background_tasks",
            healthy=False,
            message=f"Background task check failed: {str(e)}",
            duration_ms=duration_ms
        )


def check_cache_system() -> HealthCheckResult:
    """
    Check cache system connectivity and operations.
    
    Returns:
        HealthCheckResult indicating cache system status.
    """
    start_time = time.time()
    test_key = "health_check_test"
    test_value = f"test_{int(time.time())}"
    
    try:
        # Test cache write
        cache.set(test_key, test_value, 60)
        
        # Test cache read
        cached_value = cache.get(test_key)
        
        # Test cache delete
        cache.delete(test_key)
        
        duration_ms = (time.time() - start_time) * 1000
        
        if cached_value == test_value:
            return HealthCheckResult(
                name="cache",
                healthy=True,
                message="Cache operations successful",
                duration_ms=duration_ms,
                metadata={"cache_backend": str(cache.__class__)}
            )
        else:
            return HealthCheckResult(
                name="cache",
                healthy=False,
                message="Cache read/write mismatch",
                duration_ms=duration_ms
            )
            
    except Exception as e:
        duration_ms = (time.time() - start_time) * 1000
        logger.warning("Cache health check failed", extra={
            "error": str(e),
            "duration_ms": duration_ms
        })
        
        return HealthCheckResult(
            name="cache",
            healthy=False,
            message=f"Cache system error: {str(e)}",
            duration_ms=duration_ms
        )


def perform_comprehensive_health_check() -> Dict[str, Any]:
    """
    Perform comprehensive health check of all system components.
    
    Returns:
        Dictionary containing overall health status and individual check results.
    """
    overall_start = time.time()
    
    with tracer.start_as_current_span("health_check.comprehensive") as span:
        # Perform individual health checks
        checks = [
            check_database_connectivity(),
            check_background_tasks(), 
            check_cache_system(),
        ]
        
        # Calculate overall health
        all_healthy = all(check.healthy for check in checks)
        overall_duration = (time.time() - overall_start) * 1000
        
        # Add span attributes
        span.set_attribute("health.overall_status", "healthy" if all_healthy else "unhealthy")
        span.set_attribute("health.checks_count", len(checks))
        span.set_attribute("health.duration_ms", overall_duration)
        
        # Record metrics
        health_check_counter.add(1, {
            "endpoint": "comprehensive",
            "status": "healthy" if all_healthy else "unhealthy"
        })
        
        health_check_duration.record(overall_duration / 1000.0, {
            "endpoint": "comprehensive"
        })
        
        # Prepare response
        result = {
            "status": "healthy" if all_healthy else "unhealthy",
            "timestamp": timezone.now().isoformat(),
            "duration_ms": round(overall_duration, 2),
            "checks": [check.to_dict() for check in checks],
            "metadata": {
                "service": "linkding",
                "version": "1.0.0",
                "environment": getattr(settings, 'ENVIRONMENT', 'unknown')
            }
        }
        
        if all_healthy:
            span.set_status(Status(StatusCode.OK))
            logger.info("Comprehensive health check passed", extra={
                "duration_ms": overall_duration,
                "checks_passed": len(checks)
            })
        else:
            failed_checks = [check.name for check in checks if not check.healthy]
            span.set_status(Status(StatusCode.ERROR, f"Failed checks: {', '.join(failed_checks)}"))
            logger.warning("Comprehensive health check failed", extra={
                "duration_ms": overall_duration,
                "failed_checks": failed_checks
            })
        
        return result


@require_http_methods(["GET"])
def health(request):
    """
    Basic health endpoint - lightweight liveness check.
    
    This endpoint is designed for quick liveness probes and returns
    a simple JSON response indicating the service is running.
    """
    start_time = time.time()
    
    with tracer.start_as_current_span("health_check.basic") as span:
        try:
            duration_ms = (time.time() - start_time) * 1000
            
            # Record metrics
            health_check_counter.add(1, {"endpoint": "basic", "status": "healthy"})
            health_check_duration.record(duration_ms / 1000.0, {"endpoint": "basic"})
            
            span.set_attribute("health.endpoint", "basic")
            span.set_attribute("health.duration_ms", duration_ms)
            span.set_status(Status(StatusCode.OK))
            
            response_data = {
                "status": "healthy",
                "timestamp": timezone.now().isoformat(),
                "service": "linkding",
                "version": "1.0.0"
            }
            
            logger.debug("Basic health check completed", extra={
                "duration_ms": duration_ms
            })
            
            return JsonResponse(response_data)
            
        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            
            health_check_counter.add(1, {"endpoint": "basic", "status": "error"})
            health_check_duration.record(duration_ms / 1000.0, {"endpoint": "basic"})
            
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            
            logger.error("Basic health check failed", extra={
                "error": str(e),
                "duration_ms": duration_ms
            })
            
            return JsonResponse(
                {"status": "unhealthy", "error": str(e)},
                status=500
            )


@require_http_methods(["GET"])
def health_ready(request):
    """
    Readiness endpoint - comprehensive check of service dependencies.
    
    This endpoint performs thorough checks of all service dependencies
    and is suitable for Kubernetes readiness probes.
    """
    # Check cache first for recent results
    cached_result = cache.get(HEALTH_CACHE_KEY)
    if cached_result:
        logger.debug("Returning cached health check result")
        return JsonResponse(cached_result, 
                          status=200 if cached_result['status'] == 'healthy' else 503)
    
    # Perform comprehensive health check
    result = perform_comprehensive_health_check()
    
    # Cache result for performance
    cache.set(HEALTH_CACHE_KEY, result, HEALTH_CACHE_TTL)
    
    # Return appropriate status code
    status_code = 200 if result['status'] == 'healthy' else 503
    return JsonResponse(result, status=status_code)


@require_http_methods(["GET"])  
def health_live(request):
    """
    Liveness endpoint - quick check that the service is responsive.
    
    This endpoint provides a lightweight check suitable for Kubernetes
    liveness probes. It only verifies basic service responsiveness.
    """
    start_time = time.time()
    
    with tracer.start_as_current_span("health_check.liveness") as span:
        try:
            # Quick database connection test
            connections['default'].ensure_connection()
            
            duration_ms = (time.time() - start_time) * 1000
            
            # Record metrics
            health_check_counter.add(1, {"endpoint": "liveness", "status": "healthy"})
            health_check_duration.record(duration_ms / 1000.0, {"endpoint": "liveness"})
            
            span.set_attribute("health.endpoint", "liveness")
            span.set_attribute("health.duration_ms", duration_ms)
            span.set_status(Status(StatusCode.OK))
            
            response_data = {
                "status": "alive",
                "timestamp": timezone.now().isoformat(),
                "duration_ms": round(duration_ms, 2)
            }
            
            logger.debug("Liveness check completed", extra={
                "duration_ms": duration_ms
            })
            
            return JsonResponse(response_data)
            
        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            
            health_check_counter.add(1, {"endpoint": "liveness", "status": "error"})
            health_check_duration.record(duration_ms / 1000.0, {"endpoint": "liveness"})
            
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            
            logger.error("Liveness check failed", extra={
                "error": str(e),
                "duration_ms": duration_ms
            })
            
            return JsonResponse(
                {"status": "dead", "error": str(e)},
                status=500
            )


@require_http_methods(["GET"])
def health_metrics(request):
    """
    Health metrics endpoint - provides detailed health metrics and statistics.
    
    This endpoint returns detailed metrics about the health check system
    and can be used for monitoring and observability.
    """
    with tracer.start_as_current_span("health_check.metrics") as span:
        try:
            # Get database statistics
            db_stats = {
                "total_users": User.objects.count(),
                "total_bookmarks": Bookmark.objects.count(),
                "active_users_24h": User.objects.filter(
                    last_login__gte=timezone.now() - timedelta(hours=24)
                ).count(),
            }
            
            # Get background task statistics if enabled
            task_stats = {}
            if not getattr(settings, 'LD_DISABLE_BACKGROUND_TASKS', False):
                try:
                    from background_task.models import Task
                    task_stats = {
                        "pending_tasks": Task.objects.filter(failed_at__isnull=True).count(),
                        "failed_tasks": Task.objects.filter(failed_at__isnull=False).count(),
                        "completed_tasks_24h": Task.objects.filter(
                            run_at__gte=timezone.now() - timedelta(hours=24),
                            failed_at__isnull=True
                        ).count(),
                    }
                except Exception:
                    task_stats = {"error": "Unable to retrieve task statistics"}
            
            response_data = {
                "service": "linkding",
                "timestamp": timezone.now().isoformat(),
                "database_stats": db_stats,
                "task_stats": task_stats,
                "system_info": {
                    "debug_mode": settings.DEBUG,
                    "background_tasks_enabled": not getattr(settings, 'LD_DISABLE_BACKGROUND_TASKS', False),
                    "allowed_registration": getattr(settings, 'ALLOW_REGISTRATION', False)
                }
            }
            
            span.set_attribute("health.metrics_retrieved", True)
            span.set_status(Status(StatusCode.OK))
            
            return JsonResponse(response_data)
            
        except Exception as e:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            
            logger.error("Health metrics endpoint failed", extra={
                "error": str(e)
            })
            
            return JsonResponse(
                {"error": "Failed to retrieve health metrics", "details": str(e)},
                status=500
            )