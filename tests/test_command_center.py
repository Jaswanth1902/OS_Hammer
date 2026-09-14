"""
Automated Test Suite for LLM Command Center (tests/test_command_center.py).
Validates configuration loading, hardware limits, OS Hammer logic,
task-observer JSON logging, and reverse proxy routing.
"""

import os
import sys
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import httpx
from fastapi.testclient import TestClient

# Ensure parent directory is in sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config import CommandCenterConfig, settings
from logger import CommandLogger
from os_hammer import OSHammerGovernor, IDLE_PRIORITY, BELOW_NORMAL_PRIORITY, NORMAL_PRIORITY
from main import app
import proxy


def test_config_defaults_and_overrides():
    """Validates baseline config loading and env overrides."""
    # Test default settings
    assert settings.port == 11434
    assert settings.max_threads == 24
    assert settings.client_max_connections == 100
    assert settings.client_max_keepalive == 50
    assert settings.hammer_throttle_priority == "BELOW_NORMAL"
    assert "code.exe" in settings.heavy_apps
    assert "llama-server.exe" in settings.target_processes

    # Test environment variable override
    with patch.dict(os.environ, {"MAX_THREADS": "32", "PORT": "11435", "CLIENT_MAX_CONNECTIONS": "200"}):
        custom_cfg = CommandCenterConfig()
        assert custom_cfg.max_threads == 32
        assert custom_cfg.port == 11435
        assert custom_cfg.client_max_connections == 200


def test_heavy_apps_registry():
    """Verifies heavy apps list includes IDEs, browsers, and GPU/creative/CAD/rendering applications."""
    assert len(settings.heavy_apps) >= 15
    for app_name in settings.heavy_apps:
        assert app_name == app_name.lower()
        assert app_name.endswith(".exe") or "." in app_name

    # Foreground GPU & creative/CAD/rendering tools must be present
    assert "blender.exe" in settings.heavy_apps
    assert "premiere.exe" in settings.heavy_apps
    assert "photoshop.exe" in settings.heavy_apps
    assert "autocad.exe" in settings.heavy_apps
    assert "resolve.exe" in settings.heavy_apps


def test_structured_logging_for_task_observer(tmp_path):
    """Validates that rotating JSON logs match task-observer ingestion schema."""
    with patch.object(settings, "log_dir", tmp_path):
        with patch.object(settings, "log_file", "test_obs.log.jsonl"):
            test_logger = CommandLogger()
            test_logger.log_event(
                event_type="test_telemetry",
                message="Testing task-observer log output",
                endpoint="/api/generate",
                method="POST",
                status_code=200,
                latency_ms=145.2,
                active_concurrency=4,
                foreground_app="code.exe",
                hammer_active=True,
            )

            log_file = tmp_path / "test_obs.log.jsonl"
            assert log_file.exists()

            lines = log_file.read_text(encoding="utf-8").strip().splitlines()
            assert len(lines) >= 1

            record = json.loads(lines[-1])
            assert record["event_type"] == "test_telemetry"
            assert record["endpoint"] == "/api/generate"
            assert record["status_code"] == 200
            assert record["latency_ms"] == 145.2
            assert record["concurrency"]["active"] == 4
            assert record["concurrency"]["ceiling"] == 24
            assert record["system_telemetry"]["foreground_app"] == "code.exe"
            assert record["system_telemetry"]["os_hammer_engaged"] is True
            assert "task_observer" in record
            assert record["task_observer"]["predictive_scheduling_ready"] is True
            assert record["task_observer"]["throttle_incident"] is True

            # Clean up handlers so Windows releases the file handle
            for h in list(test_logger.json_logger.handlers):
                h.close()


def test_os_hammer_lifecycle_and_debouncing():
    """Validates OS Hammer priority throttling and debounce logic."""
    gov = OSHammerGovernor()
    mock_proc = MagicMock()
    
    current_prio = NORMAL_PRIORITY
    def fake_nice(new_prio=None):
        nonlocal current_prio
        if new_prio is not None:
            current_prio = new_prio
        return current_prio

    mock_proc.nice.side_effect = fake_nice

    with patch.object(gov, "find_target_processes", return_value=[mock_proc]), \
         patch.object(settings, "hammer_debounce_cycles", 2), \
         patch.object(settings, "hammer_engage_delay_sec", 0.0), \
         patch.object(settings, "hammer_release_delay_sec", 0.0), \
         patch("os_hammer.get_cpu_package_metrics", return_value=(None, 10.0)), \
         patch("os_hammer.fire_silent_ntfy"):
        # 1. First cycle: heavy tool detected (debounce cycle 1)
        with patch("os_hammer.get_foreground_process_name", return_value="code.exe"):
            gov.check_cycle()
            assert gov._consecutive_heavy_hits == 1
            assert not gov.is_hammer_active

            # 2. Second cycle: heavy tool confirmed -> hammer engages!
            gov.check_cycle()
            assert gov._consecutive_heavy_hits == 2
            assert gov.is_hammer_active
            mock_proc.nice.assert_called_with(BELOW_NORMAL_PRIORITY)

        # 3. Switching away from heavy tool (debounce cycle 1)
        with patch("os_hammer.get_foreground_process_name", return_value="explorer.exe"):
            gov.check_cycle()
            assert gov._consecutive_light_hits == 1
            assert gov.is_hammer_active

            # 4. Confirmed focus away -> hammer disengages and restores
            gov.check_cycle()
            assert gov._consecutive_light_hits == 2
            assert not gov.is_hammer_active
            mock_proc.nice.assert_called_with(NORMAL_PRIORITY)


def test_health_check_endpoint():
    """Validates that /health returns correct system status and model roster."""
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert data["service"] == "LLM_Command_Center"
        assert data["port"] == 11434
        assert data["max_threads"] == 24
        assert "client_limits" in data
        assert data["client_limits"]["max_connections"] == 100
        assert "os_hammer" in data
        assert data["upstream_url"] == "http://127.0.0.1:11435"
        assert "model_roster" in data
        assert "triage_model" in data["model_roster"]["registered_models"]
        assert "code_worker_model" in data["model_roster"]["registered_models"]
        assert "agentic_worker_model" in data["model_roster"]["registered_models"]
        assert data["model_roster"]["shared_memory_ceiling_gb"] == settings.shared_memory_ceiling_gb
        assert data["model_roster"]["sequential_loading"] == settings.sequential_loading
        assert data["model_roster"]["ollama_num_parallel"] == settings.ollama_num_parallel


def test_payload_interception_and_governance():
    """Validates dynamic model alias resolution and hardware bounds clamping in proxy."""
    # 1. Test triage_model alias resolution and options clamping
    raw_payload = json.dumps({
        "model": "triage_model",
        "prompt": "Extract JSON",
        "options": {"num_ctx": 32768, "num_thread": 16}
    }).encode("utf-8")

    governed_bytes, meta = proxy.intercept_and_govern_payload(raw_payload, "/api/generate")
    expected_triage = settings.model_roster.get("triage_model", {}).get("name", "llama3.2:3b")
    expected_thread = settings.model_roster.get("triage_model", {}).get("num_thread", 8)
    assert meta["governed"] is True
    assert meta["effective_model"] == expected_triage
    assert meta["num_ctx"] == 4096
    assert meta["num_thread"] == expected_thread

    parsed = json.loads(governed_bytes.decode("utf-8"))
    assert parsed["model"] == expected_triage
    assert parsed["options"]["num_ctx"] == 4096
    assert parsed["options"]["num_thread"] == expected_thread

    # 2. Test code_worker_model alias resolution
    code_payload = json.dumps({"model": "code_worker_model", "prompt": "def test():"}).encode("utf-8")
    gov_code_bytes, code_meta = proxy.intercept_and_govern_payload(code_payload, "/api/generate")
    expected_code_ctx = settings.model_roster.get("code_worker_model", {}).get("num_ctx", 8192)
    expected_code_th = settings.model_roster.get("code_worker_model", {}).get("num_thread", 12)
    assert code_meta["effective_model"] == "qwen2.5-coder:7b"
    assert code_meta["num_ctx"] == expected_code_ctx
    assert code_meta["num_thread"] == expected_code_th

    # 3. Test agentic_worker_model alias resolution
    agent_payload = json.dumps({"model": "agentic_worker_model", "messages": []}).encode("utf-8")
    gov_agent_bytes, agent_meta = proxy.intercept_and_govern_payload(agent_payload, "/api/chat")
    expected_agent_ctx = settings.model_roster.get("agentic_worker_model", {}).get("num_ctx", 8192)
    expected_agent_th = settings.model_roster.get("agentic_worker_model", {}).get("num_thread", 12)
    assert agent_meta["effective_model"] == "hermes3:8b"
    assert agent_meta["num_ctx"] == expected_agent_ctx
    assert agent_meta["num_thread"] == expected_agent_th


def test_ollama_version_endpoint():
    """Validates Ollama version fallback endpoint."""
    with TestClient(app) as client:
        resp = client.get("/api/version")
        assert resp.status_code == 200
        assert "version" in resp.json()


def test_proxy_upstream_offline_graceful_503():
    """Validates that when the upstream LLM runtime is offline, the proxy returns 503 instead of crashing."""
    async def mock_get_client():
        transport = httpx.MockTransport(
            lambda req: (_ for _ in ()).throw(httpx.ConnectError("Connection refused"))
        )
        return httpx.AsyncClient(transport=transport, base_url="http://mock-upstream")

    with patch.object(proxy, "get_http_client", side_effect=mock_get_client):
        with TestClient(app) as client:
            resp = client.post("/api/generate", json={"prompt": "Hello"})
            assert resp.status_code == 503
            assert "Upstream LLM server unavailable" in resp.json().get("error", "")


def test_proxy_transparent_streaming_passthrough():
    """Validates transparent streaming pass-through for LLM token streams."""
    def mock_stream_handler(request: httpx.Request) -> httpx.Response:
        chunks = [b'{"response": "Hel"}', b'{"response": "lo,"}', b'{"response": " world!"}']
        return httpx.Response(
            status_code=200,
            headers={"content-type": "application/x-ndjson"},
            content=b"\n".join(chunks),
        )

    async def mock_get_client():
        return httpx.AsyncClient(
            transport=httpx.MockTransport(mock_stream_handler),
            base_url="http://mock-upstream"
        )

    with patch.object(proxy, "get_http_client", side_effect=mock_get_client):
        with TestClient(app) as client:
            resp = client.post("/api/generate", json={"prompt": "Say hello", "stream": True})
            assert resp.status_code == 200
            assert b"world!" in resp.content


def test_schmitt_trigger_dwell_time_hysteresis():
    """Validates that Schmitt-trigger dwell time prevents premature transitions."""
    gov = OSHammerGovernor()
    mock_proc = MagicMock()
    current_prio = NORMAL_PRIORITY

    def fake_nice(new_prio=None):
        nonlocal current_prio
        if new_prio is not None:
            current_prio = new_prio
        return current_prio

    mock_proc.nice.side_effect = fake_nice

    with patch.object(gov, "find_target_processes", return_value=[mock_proc]), \
         patch.object(settings, "hammer_debounce_cycles", 2), \
         patch.object(settings, "hammer_engage_delay_sec", 1.0), \
         patch.object(settings, "hammer_release_delay_sec", 3.0), \
         patch("os_hammer.get_cpu_package_metrics", return_value=(None, 10.0)), \
         patch("os_hammer.fire_silent_ntfy"):
        with patch("os_hammer.get_foreground_process_name", return_value="code.exe"):
            # Cycle 1 at t=100.0s: 1 hit, pending engage
            gov.check_cycle(now=100.0)
            assert not gov.is_hammer_active
            assert gov.pending_state is True

            # Cycle 2 at t=100.5s: 2 hits (debounce satisfied), but elapsed=0.5s < 1.0s -> NO ENGAGE
            gov.check_cycle(now=100.5)
            assert not gov.is_hammer_active

            # Cycle 3 at t=101.1s: 3 hits, elapsed=1.1s >= 1.0s -> ENGAGE!
            gov.check_cycle(now=101.1)
            assert gov.is_hammer_active
            assert gov.total_engagements == 1
            mock_proc.nice.assert_called_with(BELOW_NORMAL_PRIORITY)

        # Switch focus away to explorer.exe (Light app)
        with patch("os_hammer.get_foreground_process_name", return_value="explorer.exe"):
            # Cycle 4 at t=102.0s: 1 light hit, pending release
            gov.check_cycle(now=102.0)
            assert gov.is_hammer_active
            assert gov.pending_state is False

            # Cycle 5 at t=103.5s: 2 light hits (debounce satisfied), but elapsed=1.5s < 3.0s (dwell protection) -> REMAINS ACTIVE
            gov.check_cycle(now=103.5)
            assert gov.is_hammer_active

            # Cycle 6 at t=105.1s: light hits sustained, elapsed=3.1s >= 3.0s -> RESTORE!
            gov.check_cycle(now=105.1)
            assert not gov.is_hammer_active
            mock_proc.nice.assert_called_with(NORMAL_PRIORITY)


def test_pid_caching_o1_lookup():
    """Validates O(1) process handle cache re-use within TTL window."""
    gov = OSHammerGovernor()
    mock_proc = MagicMock()
    mock_proc.info = {"name": "llama-server.exe", "pid": 1234}
    mock_proc.is_running.return_value = True
    mock_proc.name.return_value = "llama-server.exe"

    with patch("psutil.process_iter", return_value=[mock_proc]) as mock_iter:
        with patch.object(settings, "hammer_pid_cache_ttl_sec", 30.0):
            # 1. First lookup: cache empty -> triggers process_iter
            targets1 = gov.find_target_processes()
            assert len(targets1) == 1
            assert mock_iter.call_count == 1

            # 2. Second lookup within TTL: reuses cached process handle, no process_iter
            targets2 = gov.find_target_processes()
            assert len(targets2) == 1
            assert mock_iter.call_count == 1 # Still 1! O(1) cache hit!

            # 3. Force refresh triggers process_iter again
            targets3 = gov.find_target_processes(force_refresh=True)
            assert len(targets3) == 1
            assert mock_iter.call_count == 2


def test_delimiter_safe_ndjson_streaming():
    """Validates that fragmented TCP chunks are framed into complete newline-delimited lines."""
    # Chunk 1 cuts JSON in half, Chunk 2 completes it and starts next, Chunk 3 completes next
    raw_fragments = [
        b'{"response": "He',
        b'llo"}\n{"response": "wo',
        b'rld"}\n',
    ]

    def mock_stream_handler(request: httpx.Request) -> httpx.Response:
        async def byte_stream():
            for frag in raw_fragments:
                yield frag

        return httpx.Response(
            status_code=200,
            headers={"content-type": "application/x-ndjson"},
            content=byte_stream(),
        )

    async def mock_get_client():
        return httpx.AsyncClient(
            transport=httpx.MockTransport(mock_stream_handler),
            base_url="http://mock-upstream"
        )

    with patch.object(proxy, "get_http_client", side_effect=mock_get_client):
        with TestClient(app) as client:
            resp = client.post("/api/generate", json={"stream": True})
            assert resp.status_code == 200
            # Ensure lines in content are valid complete NDJSON lines
            lines = [line for line in resp.content.split(b"\n") if line.strip()]
            assert len(lines) == 2
            parsed1 = json.loads(lines[0])
            parsed2 = json.loads(lines[1])
            assert parsed1["response"] == "Hello"
            assert parsed2["response"] == "world"


def test_zero_dependency_prometheus_metrics_endpoint():
    """Validates that GET /metrics returns standard Prometheus exposition format."""
    with TestClient(app) as client:
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]
        text = resp.text
        assert "llm_command_center_concurrency_active" in text
        assert "llm_command_center_concurrency_max 24" in text
        assert "llm_command_center_requests_total" in text
        assert "llm_command_center_hammer_active" in text
        assert "llm_command_center_hammer_engagements_total" in text
        assert "llm_command_center_system_cpu_percent" in text
        assert "llm_command_center_system_memory_percent" in text


def test_thermal_and_graphics_hysteresis():
    """Validates that GPU/CAD/creative apps and CPU package thermal/load strain engage the OS Hammer."""
    gov = OSHammerGovernor()
    mock_proc = MagicMock()
    current_prio = NORMAL_PRIORITY

    def fake_nice(new_prio=None):
        nonlocal current_prio
        if new_prio is not None:
            current_prio = new_prio
        return current_prio

    mock_proc.nice.side_effect = fake_nice

    with patch.object(gov, "find_target_processes", return_value=[mock_proc]), \
         patch.object(settings, "hammer_debounce_cycles", 1), \
         patch.object(settings, "hammer_engage_delay_sec", 0.0), \
         patch.object(settings, "hammer_release_delay_sec", 0.0), \
         patch("os_hammer.fire_silent_ntfy"):

        # 1. Foreground Graphic app (blender.exe) triggers hammer
        with patch("os_hammer.get_foreground_process_name", return_value="blender.exe"), \
             patch("os_hammer.get_cpu_package_metrics", return_value=(45.0, 10.0)):
            gov.check_cycle()
            assert gov.is_hammer_active
            assert gov.current_throttle_reason == "blender.exe"
            mock_proc.nice.assert_called_with(BELOW_NORMAL_PRIORITY)

        # 2. Release when switching back to light app
        with patch("os_hammer.get_foreground_process_name", return_value="notepad.exe"), \
             patch("os_hammer.get_cpu_package_metrics", return_value=(45.0, 10.0)):
            gov.check_cycle()
            assert not gov.is_hammer_active
            mock_proc.nice.assert_called_with(NORMAL_PRIORITY)

        # 3. CPU Thermal strain triggers hammer even when foreground app is light
        with patch("os_hammer.get_foreground_process_name", return_value="notepad.exe"), \
             patch("os_hammer.get_cpu_package_metrics", return_value=(85.5, 40.0)):
            gov.check_cycle()
            assert gov.is_hammer_active
            assert "cpu_temp" in gov.current_throttle_reason
            mock_proc.nice.assert_called_with(BELOW_NORMAL_PRIORITY)
