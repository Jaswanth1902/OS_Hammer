"""
Configuration module for the System-Wide Local LLM Command Center.
Loads settings from config.yaml, heavy_apps.yaml, and environment variables.
"""

import os
from pathlib import Path
from typing import List, Dict, Any, Optional
import yaml

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
HEAVY_APPS_PATH = BASE_DIR / "heavy_apps.yaml"


class CommandCenterConfig:
    """Encapsulates all runtime configuration parameters."""

    def __init__(self, config_file: Optional[Path] = None):
        self.config_file = config_file or CONFIG_PATH
        self.raw_data: Dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        """Reads config.yaml and applies environment overrides."""
        if self.config_file.exists():
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    self.raw_data = yaml.safe_load(f) or {}
            except Exception as e:
                print(f"[CONFIG] Warning: Failed to parse {self.config_file}: {e}")
                self.raw_data = {}
        else:
            self.raw_data = {}

        # Network bindings
        self.host: str = os.getenv("HOST", self.raw_data.get("host", "127.0.0.1"))
        self.port: int = int(os.getenv("PORT", self.raw_data.get("port", 11434)))
        self.upstream_url: str = os.getenv(
            "UPSTREAM_URL",
            self.raw_data.get("upstream_url", "http://127.0.0.1:11435")
        ).rstrip("/")

        # Local Three-Model Inference Roster
        roster_cfg = self.raw_data.get("model_roster", {})
        self.upstream_engine: str = roster_cfg.get("upstream_engine", "ollama")
        self.shared_memory_ceiling_gb: float = float(
            roster_cfg.get("shared_memory_ceiling_gb", 8.0)
        )
        self.sequential_loading: bool = bool(
            roster_cfg.get("sequential_loading", False)
        )
        self.ollama_num_parallel: int = int(
            os.getenv("OLLAMA_NUM_PARALLEL", roster_cfg.get("ollama_num_parallel", 3))
        )
        self.model_roster: Dict[str, Dict[str, Any]] = roster_cfg.get("models", {
            "triage_model": {
                "name": "llama3.2:3b",
                "alias": "triage_model",
                "role": "Pacing, JSON schema extraction, regex transforms, rapid blueprint synthesis",
                "num_ctx": 2048,
                "num_thread": 8,
                "num_predict": 350,
                "max_memory_mb": 2500,
            },
            "code_worker_model": {
                "name": "qwen2.5-coder:7b",
                "alias": "code_worker_model",
                "role": "AST surgical refactoring, TDD fixtures",
                "num_ctx": 4096,
                "num_thread": 8,
                "num_predict": 500,
                "max_memory_mb": 5500,
            },
            "agentic_worker_model": {
                "name": "hermes3:8b",
                "alias": "agentic_worker_model",
                "role": "NVIDIA Hermes Agent, MCP tool bindings, complex fallback synthesis",
                "num_ctx": 4096,
                "num_thread": 8,
                "num_predict": 500,
                "max_memory_mb": 6000,
            },
        })
        self.model_aliases: Dict[str, str] = {
            "triage_model": self.model_roster.get("triage_model", {}).get("name", "llama3.2:3b"),
            "code_worker_model": self.model_roster.get("code_worker_model", {}).get("name", "qwen2.5-coder:7b"),
            "agentic_worker_model": self.model_roster.get("agentic_worker_model", {}).get("name", "hermes3:8b"),
            "triage": self.model_roster.get("triage_model", {}).get("name", "llama3.2:3b"),
            "code_worker": self.model_roster.get("code_worker_model", {}).get("name", "qwen2.5-coder:7b"),
            "agentic_worker": self.model_roster.get("agentic_worker_model", {}).get("name", "hermes3:8b"),
            "hermes": self.model_roster.get("agentic_worker_model", {}).get("name", "hermes3:8b"),
        }

        # Hardware limits & Dynamic Concurrency Governor
        self.max_threads: int = int(
            os.getenv("MAX_THREADS", self.raw_data.get("max_threads", 24))
        )
        self.request_timeout: float = float(
            os.getenv("REQUEST_TIMEOUT", self.raw_data.get("request_timeout", 450.0))
        )
        self.connect_timeout: float = float(
            os.getenv("CONNECT_TIMEOUT", self.raw_data.get("connect_timeout", 10.0))
        )
        self.client_max_connections: int = int(
            os.getenv(
                "CLIENT_MAX_CONNECTIONS",
                self.raw_data.get("client_max_connections", 100),
            )
        )
        self.client_max_keepalive: int = int(
            os.getenv(
                "CLIENT_MAX_KEEPALIVE",
                self.raw_data.get("client_max_keepalive", 50),
            )
        )

        # OS Hammer Configuration
        hammer_cfg = self.raw_data.get("hammer", {})
        self.hammer_enabled: bool = (
            os.getenv("HAMMER_ENABLED", str(hammer_cfg.get("enabled", True))).lower()
            in ("true", "1", "yes")
        )
        self.hammer_poll_interval: float = float(
            os.getenv(
                "HAMMER_POLL_INTERVAL",
                hammer_cfg.get("poll_interval_sec", 0.5),
            )
        )
        self.hammer_debounce_cycles: int = int(
            os.getenv(
                "HAMMER_DEBOUNCE_CYCLES",
                hammer_cfg.get("debounce_cycles", 2),
            )
        )
        self.hammer_engage_delay_sec: float = float(
            os.getenv(
                "HAMMER_ENGAGE_DELAY_SEC",
                hammer_cfg.get("engage_delay_sec", 1.0),
            )
        )
        self.hammer_release_delay_sec: float = float(
            os.getenv(
                "HAMMER_RELEASE_DELAY_SEC",
                hammer_cfg.get("release_delay_sec", 2.0),
            )
        )
        self.hammer_pid_cache_ttl_sec: float = float(
            os.getenv(
                "HAMMER_PID_CACHE_TTL_SEC",
                hammer_cfg.get("pid_cache_ttl_sec", 30.0),
            )
        )
        self.hammer_throttle_priority: str = os.getenv(
            "HAMMER_THROTTLE_PRIORITY",
            hammer_cfg.get("throttle_priority", "BELOW_NORMAL"),
        ).upper()
        self.hammer_normal_priority: str = os.getenv(
            "HAMMER_NORMAL_PRIORITY",
            hammer_cfg.get("normal_priority", "NORMAL"),
        ).upper()
        self.target_processes: List[str] = [
            p.lower()
            for p in hammer_cfg.get(
                "target_processes",
                ["llama-server.exe", "ollama_llama_server.exe", "vllm.exe", "vllm"],
            )
        ]
        self.heavy_apps_file: Path = BASE_DIR / hammer_cfg.get(
            "heavy_apps_file", "heavy_apps.yaml"
        )
        self.heavy_apps: List[str] = self._load_heavy_apps()
        self.hammer_cpu_load_threshold: float = float(
            os.getenv(
                "HAMMER_CPU_LOAD_THRESHOLD",
                hammer_cfg.get("cpu_load_threshold_percent", 85.0),
            )
        )
        self.hammer_cpu_temp_threshold: float = float(
            os.getenv(
                "HAMMER_CPU_TEMP_THRESHOLD",
                hammer_cfg.get("cpu_temp_threshold_c", 80.0),
            )
        )
        self.hammer_check_cpu_package: bool = bool(
            hammer_cfg.get("check_cpu_package", True)
        )
        self.hammer_check_foreground_graphics: bool = bool(
            hammer_cfg.get("check_foreground_graphics", True)
        )

        # Ntfy Configuration
        ntfy_cfg = self.raw_data.get("ntfy", {})
        self.ntfy_enabled: bool = (
            os.getenv("NTFY_ENABLED", str(ntfy_cfg.get("enabled", True))).lower()
            in ("true", "1", "yes")
        )
        self.ntfy_url: str = os.getenv(
            "NTFY_URL",
            ntfy_cfg.get("url", "http://localhost:8080/llm-command-centre"),
        )
        self.ntfy_silent_priority: str = ntfy_cfg.get("silent_priority", "min")
        self.ntfy_timeout: float = float(
            os.getenv("NTFY_TIMEOUT", ntfy_cfg.get("timeout_sec", 2.0))
        )

        # Logging Configuration
        log_cfg = self.raw_data.get("logging", {})
        log_dir_name = os.getenv("LOG_DIR", log_cfg.get("log_dir", "logs"))
        self.log_dir: Path = BASE_DIR / log_dir_name
        self.log_file: str = log_cfg.get("log_file", "command_center.log.jsonl")
        self.log_max_bytes: int = int(log_cfg.get("max_bytes", 10 * 1024 * 1024))
        self.log_backup_count: int = int(log_cfg.get("backup_count", 5))
        self.log_level: str = log_cfg.get("level", "INFO")

    def _load_heavy_apps(self) -> List[str]:
        """Loads list of heavy apps from YAML file."""
        if not self.heavy_apps_file.exists():
            return ["code.exe", "devenv.exe", "chrome.exe", "msedge.exe", "blender.exe"]
        try:
            with open(self.heavy_apps_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                apps = data.get("heavy_apps", [])
                return [str(app).strip().lower() for app in apps if app]
        except Exception as e:
            print(f"[CONFIG] Warning: Could not load {self.heavy_apps_file}: {e}")
            return ["code.exe", "devenv.exe", "chrome.exe", "msedge.exe"]


# Global instance
settings = CommandCenterConfig()
