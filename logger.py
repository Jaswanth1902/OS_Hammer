"""
Structured Rotating JSON Lines Logger for LLM Command Center.
Produces machine-readable telemetry specifically formatted for task-observer ingestion
and predictive scheduling.
"""

import os
import sys
import json
import uuid
import time
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional
import psutil

from config import settings


class TaskObserverJSONFormatter(logging.Formatter):
    """Formats log records as valid, single-line JSON Lines for task-observer."""

    def format(self, record: logging.LogRecord) -> str:
        data = getattr(record, "structured_data", None)
        if data is None:
            # Fallback for plain log statements
            data = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event_type": "internal_log",
                "level": record.levelname,
                "message": record.getMessage(),
                "logger": record.name,
            }
        return json.dumps(data, ensure_ascii=False)


class CommandLogger:
    """Manages rotating file handlers and telemetry emission."""

    def __init__(self):
        self.log_dir = settings.log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.log_dir / settings.log_file

        # Configure JSON Lines rotating logger
        self.json_logger = logging.getLogger("command_center_json")
        self.json_logger.setLevel(logging.INFO)
        self.json_logger.propagate = False

        # Clear existing handlers to prevent duplication
        if self.json_logger.hasHandlers():
            self.json_logger.handlers.clear()

        # Rotating file handler (10MB, 5 backups)
        file_handler = RotatingFileHandler(
            str(self.log_path),
            maxBytes=settings.log_max_bytes,
            backupCount=settings.log_backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(TaskObserverJSONFormatter())
        self.json_logger.addHandler(file_handler)

        # Standard console logger
        self.console_logger = logging.getLogger("command_center_console")
        self.console_logger.setLevel(logging.INFO)
        if not self.console_logger.hasHandlers():
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setFormatter(
                logging.Formatter(
                    "[%(asctime)s] [%(levelname)s] [COMMAND_CENTER] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
            self.console_logger.addHandler(console_handler)

    def log_event(
        self,
        event_type: str,
        message: str,
        request_id: Optional[str] = None,
        endpoint: Optional[str] = None,
        method: Optional[str] = None,
        status_code: Optional[int] = None,
        latency_ms: Optional[float] = None,
        is_stream: bool = False,
        active_concurrency: int = 0,
        foreground_app: Optional[str] = None,
        hammer_active: bool = False,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Emits a structured JSON event to the rotating log and a summary to console."""
        now_utc = datetime.now(timezone.utc).isoformat()

        # Gather lightweight system metrics
        cpu_usage = 0.0
        ram_usage = 0.0
        try:
            cpu_usage = psutil.cpu_percent(interval=None)
            ram_usage = psutil.virtual_memory().percent
        except Exception:
            pass

        # Structured schema for task-observer ingestion
        record: Dict[str, Any] = {
            "timestamp": now_utc,
            "event_type": event_type,
            "message": message,
            "request_id": request_id or str(uuid.uuid4()),
            "endpoint": endpoint,
            "method": method,
            "status_code": status_code,
            "latency_ms": round(latency_ms, 2) if latency_ms is not None else None,
            "is_stream": is_stream,
            "concurrency": {
                "active": active_concurrency,
                "ceiling": settings.max_threads,
                "saturated": active_concurrency >= settings.max_threads,
            },
            "system_telemetry": {
                "cpu_percent": cpu_usage,
                "ram_percent": ram_usage,
                "foreground_app": foreground_app or "unknown",
                "os_hammer_engaged": hammer_active,
            },
            "task_observer": {
                "schema_version": "1.0",
                "domain": "05_Services/LLM_Command_Center",
                "predictive_scheduling_ready": True,
                "throttle_incident": hammer_active,
                "latency_anomaly": (latency_ms is not None and latency_ms > 30000.0),
            },
            "details": extra or {},
        }

        # Write to rotating JSON Lines log
        log_record = self.json_logger.makeRecord(
            name=self.json_logger.name,
            level=logging.INFO,
            fn="logger.py",
            lno=120,
            msg=message,
            args=(),
            exc_info=None,
        )
        log_record.structured_data = record
        self.json_logger.handle(log_record)

        # Write concise summary to console
        self.console_logger.info(f"{event_type.upper()}: {message}")


# Global logger instance
telemetry = CommandLogger()
