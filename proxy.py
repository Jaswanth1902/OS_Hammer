"""
Universal Reverse Proxy for local LLM runtimes (llama-server.exe / vLLM / llama.cpp).
Transparently intercepts requests on port 11434, enforces MAX_THREADS concurrency limits,
streams tokens via SSE / chunked transfer, and emits structured telemetry.
"""

import json
import time
import uuid
import asyncio
from contextlib import nullcontext
from typing import AsyncGenerator, Dict, Any, Optional, Tuple
import httpx
from fastapi import Request, Response
from fastapi.responses import StreamingResponse, JSONResponse

from config import settings
from logger import telemetry
from os_hammer import governor

# Dynamic Swarm Concurrency Gate for Inference
# When sequential_loading is True, restricts to 1. When False, allows concurrent speculative
# worker branches up to settings.ollama_num_parallel (default 3) within 2 GB VRAM bounds.
inference_semaphore: Optional[asyncio.Semaphore] = None


def get_inference_semaphore() -> asyncio.Semaphore:
    """Lazily initializes the inference concurrency semaphore."""
    global inference_semaphore
    if inference_semaphore is None:
        limit = 1 if settings.sequential_loading else max(1, getattr(settings, "ollama_num_parallel", 3))
        inference_semaphore = asyncio.Semaphore(limit)
    return inference_semaphore

# Concurrency Gate
concurrency_semaphore: Optional[asyncio.Semaphore] = None
active_request_counter: int = 0
total_requests_counter: int = 0
counter_lock = asyncio.Lock()

# Persistent HTTP Client pool
http_client: Optional[httpx.AsyncClient] = None

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
}


def get_concurrency_semaphore() -> asyncio.Semaphore:
    """Lazily initializes the semaphore in the running event loop."""
    global concurrency_semaphore
    if concurrency_semaphore is None:
        concurrency_semaphore = asyncio.Semaphore(settings.max_threads)
    return concurrency_semaphore


async def get_http_client() -> httpx.AsyncClient:
    """Returns or initializes shared httpx client."""
    global http_client
    if http_client is None or http_client.is_closed:
        http_client = httpx.AsyncClient(
            base_url=settings.upstream_url,
            timeout=httpx.Timeout(settings.request_timeout, connect=settings.connect_timeout),
            limits=httpx.Limits(
                max_keepalive_connections=settings.client_max_keepalive,
                max_connections=settings.client_max_connections,
            ),
        )
    return http_client


async def close_http_client() -> None:
    """Closes the shared client on shutdown."""
    global http_client
    if http_client is not None and not http_client.is_closed:
        await http_client.aclose()


def sanitize_headers(request_headers: Dict[str, str]) -> Dict[str, str]:
    """Filters out hop-by-hop headers for transparent proxy forwarding."""
    return {
        k: v
        for k, v in request_headers.items()
        if k.lower() not in HOP_BY_HOP_HEADERS
    }


def intercept_and_govern_payload(body_bytes: bytes, target_path: str) -> Tuple[bytes, Dict[str, Any]]:
    """
    Inspects and rewrites JSON payloads:
    - Resolves alias models (triage_model -> llama3.2:3b, code_worker_model -> qwen2.5-coder:7b, agentic_worker_model -> hermes3:8b)
    - Enforces context window limits (num_ctx: 4096 or 8192)
    - Enforces CPU thread limits (num_thread: 4 or 8)
    - Prevents multi-model memory blowups exceeding the 8.0 GB shared container limit
    """
    if not body_bytes:
        return body_bytes, {}

    try:
        data = json.loads(body_bytes.decode("utf-8"))
        if not isinstance(data, dict):
            return body_bytes, {}
    except Exception:
        return body_bytes, {}

    governed = False
    original_model = str(data.get("model", "")).strip()
    target_model = original_model

    # Resolve alias if present
    if original_model in settings.model_aliases:
        target_model = settings.model_aliases[original_model]
        data["model"] = target_model
        governed = True

    # Identify matching config for model bounds
    matching_spec = None
    for alias_key, spec in settings.model_roster.items():
        if spec.get("name") == target_model or alias_key == original_model:
            matching_spec = spec
            break

    # If no specific spec matched, default to conservative bounds
    num_ctx_ceiling = matching_spec.get("num_ctx", 4096) if matching_spec else 4096
    num_thread_ceiling = matching_spec.get("num_thread", 8) if matching_spec else 8
    num_predict_ceiling = matching_spec.get("num_predict", 1024) if matching_spec else 1024
    
    current_requested_ctx = data.get("options", {}).get("num_ctx") if isinstance(data.get("options"), dict) else None
    
    # === LLMFit Live Telemetry Check (Integrated Graphics / System RAM) ===
    try:
        import psutil
        
        # 1. Pull Live System RAM (Shared Memory)
        mem_info = psutil.virtual_memory()
        free_ram_mb = mem_info.available / (1024 * 1024)
            
        # 2. Heuristic LLMFit Memory Calculation (Model Size + KV Cache)
        # Assuming typical 4-bit/8-bit quantizations loaded into system RAM
        required_ram = 4096  # Baseline 4GB
        if "8b" in target_model.lower() or "7b" in target_model.lower():
            required_ram = 6000
        elif "14b" in target_model.lower() or "32b" in target_model.lower():
            required_ram = 14000
            
        ctx_overhead = ((current_requested_ctx or num_ctx_ceiling) / 8192) * 1024
        required_ram += ctx_overhead
        
        # 3. Dynamic Model Routing
        if free_ram_mb < required_ram:
            telemetry.console_logger.warning(f"[LLMFit] OOM RISK DETECTED: {target_model} requires {required_ram}MB RAM but only {free_ram_mb:.0f}MB is free.")
            if "coder" in target_model.lower():
                target_model = "qwen2.5-coder:1.5b"
            else:
                target_model = "qwen2.5:1.5b"
            data["model"] = target_model
            governed = True
            telemetry.console_logger.warning(f"[LLMFit] Dynamic Fallback Engaged -> {target_model}")
            
    except Exception as e:
        telemetry.console_logger.error(f"[LLMFit] CPU/RAM Telemetry probe failed: {e}")
    # =======================================================================

    # Apply limits to Ollama options payload
    options = data.get("options", {})
    if not isinstance(options, dict):
        options = {}

    current_ctx = options.get("num_ctx")
    if current_ctx is None or current_ctx > num_ctx_ceiling:
        options["num_ctx"] = num_ctx_ceiling
        governed = True

    current_threads = options.get("num_thread")
    if current_threads is None or current_threads > num_thread_ceiling:
        options["num_thread"] = num_thread_ceiling
        governed = True

    current_gpu = options.get("num_gpu")
    if current_gpu is None or current_gpu != 0:
        options["num_gpu"] = 0
        governed = True

    current_predict = options.get("num_predict")
    if current_predict is None or current_predict > num_predict_ceiling:
        options["num_predict"] = num_predict_ceiling
        governed = True

    data["options"] = options

    # OpenAI-compatible /v1/ endpoints parameter support (max_tokens / n_threads)
    if "max_tokens" in data and data["max_tokens"] > num_predict_ceiling:
        data["max_tokens"] = num_predict_ceiling
        governed = True

    new_bytes = json.dumps(data).encode("utf-8") if governed else body_bytes
    metadata = {
        "original_model": original_model,
        "effective_model": target_model,
        "num_ctx": options.get("num_ctx"),
        "num_thread": options.get("num_thread"),
        "num_predict": options.get("num_predict"),
        "governed": governed
    }
    return new_bytes, metadata


async def forward_request(path: str, request: Request) -> Response:
    """
    Transparently forwards an incoming HTTP request to the upstream LLM backend.
    Enforces concurrency ceiling via Semaphore, sequential model loading lock,
    payload governance, and streams responses.
    """
    global active_request_counter, total_requests_counter
    req_id = str(uuid.uuid4())
    start_time = time.perf_counter()
    sem = get_concurrency_semaphore()
    client = await get_http_client()

    target_path = f"/{path.lstrip('/')}"
    query_str = str(request.url.query)
    full_url = f"{target_path}?{query_str}" if query_str else target_path

    # Filter inbound headers
    forward_headers = sanitize_headers(dict(request.headers))

    # Read body bytes
    body = await request.body()

    # Apply dynamic payload interception & model roster governance
    governed_body, gov_meta = intercept_and_govern_payload(body, target_path)
    if gov_meta.get("governed"):
        forward_headers["content-length"] = str(len(governed_body))
        body = governed_body

    # Track concurrency entering the gate
    async with counter_lock:
        active_request_counter += 1
        total_requests_counter += 1
        current_concurrency = active_request_counter

    telemetry.log_event(
        event_type="request_start",
        message=f"Routing {request.method} {target_path} (Model: {gov_meta.get('effective_model', 'default')}, Concurrency: {current_concurrency}/{settings.max_threads})",
        request_id=req_id,
        endpoint=target_path,
        method=request.method,
        active_concurrency=current_concurrency,
        foreground_app=governor.current_foreground_app,
        hammer_active=governor.is_hammer_active,
        extra=gov_meta,
    )

    try:
        # Only inference endpoints require the sequential loading lock.
        # Status/metadata endpoints (/api/tags, /api/ps, /api/version, GET requests)
        # must NEVER queue behind a running inference — they resolve instantly on Ollama.
        INFERENCE_PATHS = {"/api/generate", "/api/chat", "/v1/completions", "/v1/chat/completions"}
        is_inference = request.method == "POST" and any(target_path.startswith(p) for p in INFERENCE_PATHS)
        inf_ctx = get_inference_semaphore() if is_inference else nullcontext()

        async with inf_ctx:
            async with sem:
                # Build upstream request
                upstream_req = client.build_request(
                    method=request.method,
                    url=full_url,
                    headers=forward_headers,
                    content=body,
                )

                try:
                    upstream_resp = await client.send(upstream_req, stream=True)
                except (httpx.ConnectError, httpx.ConnectTimeout) as conn_err:
                    latency_ms = (time.perf_counter() - start_time) * 1000
                    telemetry.log_event(
                        event_type="upstream_error",
                        message=f"Failed connecting to upstream {settings.upstream_url}: {conn_err}",
                        request_id=req_id,
                        endpoint=target_path,
                        method=request.method,
                        status_code=503,
                        latency_ms=latency_ms,
                        active_concurrency=current_concurrency,
                        foreground_app=governor.current_foreground_app,
                        hammer_active=governor.is_hammer_active,
                    )
                    return JSONResponse(
                        status_code=503,
                        content={
                            "error": "Upstream LLM server unavailable",
                            "upstream_url": settings.upstream_url,
                            "detail": str(conn_err),
                        },
                    )
                except Exception as exc:
                    latency_ms = (time.perf_counter() - start_time) * 1000
                    telemetry.log_event(
                        event_type="upstream_exception",
                        message=f"Exception during upstream dispatch: {exc}",
                        request_id=req_id,
                        endpoint=target_path,
                        method=request.method,
                        status_code=502,
                        latency_ms=latency_ms,
                        active_concurrency=current_concurrency,
                        foreground_app=governor.current_foreground_app,
                        hammer_active=governor.is_hammer_active,
                    )
                    return JSONResponse(
                        status_code=502,
                        content={"error": "Bad Gateway", "detail": str(exc)},
                    )

                # Check if upstream response is a streaming response
                content_type = upstream_resp.headers.get("content-type", "")
                is_streaming = (
                    "text/event-stream" in content_type
                    or "application/x-ndjson" in content_type
                    or "chunked" in upstream_resp.headers.get("transfer-encoding", "")
                )

                resp_headers = {
                    k: v
                    for k, v in upstream_resp.headers.items()
                    if k.lower() not in HOP_BY_HOP_HEADERS
                }

                if not is_streaming:
                    # Non-streaming full response — read while STILL holding inference semaphore!
                    # This guarantees Ollama has finished token generation before releasing the lock.
                    resp_content = await upstream_resp.aread()
                    await upstream_resp.aclose()
                    latency_ms = (time.perf_counter() - start_time) * 1000

                    telemetry.log_event(
                        event_type="request_end",
                        message=f"Completed {request.method} {target_path} (Status: {upstream_resp.status_code}, Latency: {latency_ms:.1f}ms)",
                        request_id=req_id,
                        endpoint=target_path,
                        method=request.method,
                        status_code=upstream_resp.status_code,
                        latency_ms=latency_ms,
                        is_stream=False,
                        active_concurrency=active_request_counter,
                        foreground_app=governor.current_foreground_app,
                        hammer_active=governor.is_hammer_active,
                    )

                    return Response(
                        content=resp_content,
                        status_code=upstream_resp.status_code,
                        headers=resp_headers,
                        media_type=content_type,
                    )

            # Streaming response handling
            async def stream_generator() -> AsyncGenerator[bytes, None]:
                buffer = b""
                try:
                    async for chunk in upstream_resp.aiter_bytes():
                        buffer += chunk
                        while b"\n" in buffer:
                            line, buffer = buffer.split(b"\n", 1)
                            yield line + b"\n"
                    if buffer:
                        yield buffer
                except (asyncio.CancelledError, GeneratorExit):
                    pass
                finally:
                    await upstream_resp.aclose()
                    latency_ms = (time.perf_counter() - start_time) * 1000
                    telemetry.log_event(
                        event_type="request_end",
                        message=f"Completed streaming {request.method} {target_path} (Status: {upstream_resp.status_code}, Latency: {latency_ms:.1f}ms)",
                        request_id=req_id,
                        endpoint=target_path,
                        method=request.method,
                        status_code=upstream_resp.status_code,
                        latency_ms=latency_ms,
                        is_stream=True,
                        active_concurrency=active_request_counter,
                        foreground_app=governor.current_foreground_app,
                        hammer_active=governor.is_hammer_active,
                    )

            return StreamingResponse(
                stream_generator(),
                status_code=upstream_resp.status_code,
                headers=resp_headers,
                media_type=content_type,
            )

    finally:
        async with counter_lock:
            active_request_counter = max(0, active_request_counter - 1)
