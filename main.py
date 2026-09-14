"""
Main entry point for System-Wide Local LLM Command Center.
Initializes FastAPI, lifecycle hooks for OS Hammer Governor, and wildcard reverse proxy routing.
"""

import sys
import argparse
from contextlib import asynccontextmanager
from typing import Dict, Any

import psutil
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from config import settings
from logger import telemetry
from os_hammer import governor
import proxy


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle manager orchestrating background governor and connection pools."""
    telemetry.console_logger.info(
        f"Starting LLM Command Center on {settings.host}:{settings.port} (Upstream: {settings.upstream_url}, MAX_THREADS: {settings.max_threads})"
    )

    # 1. Start OS Hammer monitoring thread
    governor.start()

    # 2. Warm up HTTP client pool
    await proxy.get_http_client()

    telemetry.log_event(
        event_type="service_started",
        message=f"LLM Command Center bound to {settings.host}:{settings.port}. Transparent proxy active.",
        extra={
            "upstream": settings.upstream_url,
            "max_threads": settings.max_threads,
            "hammer_enabled": settings.hammer_enabled,
        },
    )

    yield

    # Shutdown sequence
    telemetry.console_logger.info("Initiating graceful shutdown...")
    governor.stop()
    await proxy.close_http_client()
    telemetry.log_event(
        event_type="service_stopped",
        message="LLM Command Center daemon stopped cleanly.",
    )


app = FastAPI(
    title="Antigravity Local LLM Command Center",
    description="Universal transparent reverse proxy and OS-level governor for local LLM runtimes",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health_check() -> Dict[str, Any]:
    """Health check endpoint required by service_manager.py and monitoring probes."""
    return {
        "status": "healthy",
        "service": "LLM_Command_Center",
        "version": "1.0.0",
        "port": settings.port,
        "max_threads": settings.max_threads,
        "active_requests": proxy.active_request_counter,
        "client_limits": {
            "max_connections": settings.client_max_connections,
            "max_keepalive": settings.client_max_keepalive,
        },
        "os_hammer": {
            "enabled": settings.hammer_enabled,
            "active": governor.is_hammer_active,
            "foreground_app": governor.current_foreground_app,
            "throttle_priority": settings.hammer_throttle_priority,
            "normal_priority": settings.hammer_normal_priority,
            "cpu_load_percent": governor.current_cpu_load,
            "cpu_temp_c": governor.current_cpu_temp,
            "throttle_reason": governor.current_throttle_reason,
        },
        "model_roster": {
            "upstream_engine": settings.upstream_engine,
            "shared_memory_ceiling_gb": settings.shared_memory_ceiling_gb,
            "sequential_loading": settings.sequential_loading,
            "ollama_num_parallel": settings.ollama_num_parallel,
            "registered_models": list(settings.model_roster.keys()),
            "aliases": settings.model_aliases,
        },
        "upstream_url": settings.upstream_url,
    }


@app.get("/api/version")
async def ollama_version() -> Dict[str, Any]:
    """Provides Ollama-compatible version endpoint fallback."""
    return {"version": "0.1.34-command-center"}


@app.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics() -> PlainTextResponse:
    """
    Exposes zero-dependency Prometheus text exposition format metrics.
    Compatible with Prometheus, VictoriaMetrics, Grafana Agent, and OpenTelemetry collectors.
    """
    try:
        cpu_usage = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        mem_percent = mem.percent
    except Exception:
        cpu_usage = 0.0
        mem_percent = 0.0

    lines = [
        "# HELP llm_command_center_concurrency_active Current active LLM inference streams",
        "# TYPE llm_command_center_concurrency_active gauge",
        f"llm_command_center_concurrency_active {proxy.active_request_counter}",
        "# HELP llm_command_center_concurrency_max Configured maximum concurrency ceiling",
        "# TYPE llm_command_center_concurrency_max gauge",
        f"llm_command_center_concurrency_max {settings.max_threads}",
        "# HELP llm_command_center_requests_total Total incoming proxy requests handled",
        "# TYPE llm_command_center_requests_total counter",
        f"llm_command_center_requests_total {proxy.total_requests_counter}",
        "# HELP llm_command_center_hammer_active OS Hammer throttling active status (1=active, 0=inactive)",
        "# TYPE llm_command_center_hammer_active gauge",
        f"llm_command_center_hammer_active {1 if governor.is_hammer_active else 0}",
        "# HELP llm_command_center_hammer_engagements_total Total times OS Hammer throttled target runtimes",
        "# TYPE llm_command_center_hammer_engagements_total counter",
        f"llm_command_center_hammer_engagements_total {governor.total_engagements}",
        "# HELP llm_command_center_system_cpu_percent System CPU usage percent",
        "# TYPE llm_command_center_system_cpu_percent gauge",
        f"llm_command_center_system_cpu_percent {cpu_usage:.1f}",
        "# HELP llm_command_center_system_memory_percent System memory usage percent",
        "# TYPE llm_command_center_system_memory_percent gauge",
        f"llm_command_center_system_memory_percent {mem_percent:.1f}",
        "",
    ]
    return PlainTextResponse(
        "\n".join(lines),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD", "PATCH"])
async def catch_all_proxy(path: str, request: Request) -> Response:
    """Catches all incoming client traffic and forwards it to the upstream LLM runtime."""
    return await proxy.forward_request(path, request)


def main():
    parser = argparse.ArgumentParser(
        description="System-Wide Local LLM Command Center Daemon"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Perform preflight configuration checks and exit immediately",
    )
    args = parser.parse_args()

    if args.dry_run:
        print("[DRY-RUN] Preflight checks passing:")
        print(f"  - Host: {settings.host}")
        print(f"  - Port: {settings.port}")
        print(f"  - Upstream URL: {settings.upstream_url}")
        print(f"  - MAX_THREADS (Concurrency Ceiling): {settings.max_threads}")
        print(f"  - Client Max Connections: {settings.client_max_connections} (Keepalive: {settings.client_max_keepalive})")
        print(f"  - Throttle Priority: {settings.hammer_throttle_priority}")
        print(f"  - Normal Priority: {settings.hammer_normal_priority}")
        print(f"  - Heavy Apps Count: {len(settings.heavy_apps)}")
        print(f"  - Target LLM Processes: {settings.target_processes}")
        print(f"  - Log File: {telemetry.log_path}")
        sys.exit(0)

    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main()
