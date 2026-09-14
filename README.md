# 🔨 OS-Hammer
### *Local LLM Reverse Proxy & Preemptive OS Priority Governor*

[![License: MIT](https://img.shields.io/badge/License-MIT-C5A059.svg?style=flat-square)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-121110.svg?style=flat-square&logo=python&logoColor=C5A059)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-High%20Throughput-4A6B5D.svg?style=flat-square)](https://fastapi.tiangolo.com/)
[![Tests: 14/14 Pass](https://img.shields.io/badge/Tests-14%2F14%20Passing-C86D51.svg?style=flat-square)](https://github.com/Jaswanth1902/OS_Hammer)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                                 OS-HAMMER                                   │
│            Local LLM Reverse Proxy (:11434) & Process Governor              │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
            ┌──────────────────────────┴──────────────────────────┐
            ▼                                                     ▼
 [Preemptive Priority Governor]                       [Transparent Reverse Proxy]
 • Monitors foreground focus (win32gui/psutil)        • Proxies Ollama & OpenAI APIs
 • Detects 35+ heavy apps (VS Code, Chrome, 3D)       • Hardware concurrency semaphore
 • Schmitt-trigger hysteresis dwell timer             • Delimiter-safe NDJSON streaming
 • Throttles llama-server to IDLE priority            • Tier-0 Prometheus /metrics text
```

---

## ⚡ The Problem: Desktop Freezes During Local LLM Inference

When running local LLMs (Ollama, llama.cpp, vLLM) on consumer workstations, heavy background inference models consume all CPU/GPU compute threads. The result:
- **VS Code and Cursor lock up** during autocomplete or generation.
- **Mouse cursor stutters** and audio drops frames.
- **Alt-Tabbing between IDE and browser** causes extreme UI sluggishness.

**OS-Hammer** solves this at the kernel level. It sits as a transparent reverse proxy in front of your local LLM engine, constantly watching user foreground activity. The instant you focus on an IDE, 3D suite, or compiler, OS-Hammer dynamically throttles the model process to `IDLE_PRIORITY_CLASS`. When you step away, it instantly restores `NORMAL_PRIORITY_CLASS`.

---

## 🌟 Key Features

1. **Preemptive OS Hammer Priority Throttling**:
   - Monitors active foreground window (`win32gui`/`psutil`) against 35+ configured heavy developer and creative tools (VS Code, Cursor, Visual Studio, Chrome, Blender, Unreal Engine, Premiere, Photoshop).
   - Throttles `llama-server.exe` / `ollama.exe` to `IDLE_PRIORITY_CLASS` (64) upon heavy tool focus; restores to `NORMAL_PRIORITY_CLASS` (32) on focus release.
2. **Schmitt-Trigger Cooldown Hysteresis**:
   - Dual-threshold dwell timer (`engage_delay_sec: 1.0`, `release_delay_sec: 3.0`) eliminates priority flapping when rapidly switching windows.
3. **$O(1)$ Process PID Caching**:
   - Employs cached process handles with a 30s TTL, avoiding repeated $O(N)$ system-wide process table scans (`psutil.process_iter()`).
4. **Delimiter-Safe NDJSON Stream Framing**:
   - Buffers raw TCP byte fragments and yields strictly on complete newline boundaries, preventing split JSON tokens across SSE streams.
5. **Hardware Concurrency Governor**:
   - Calibrated semaphore (`MAX_THREADS: 24`, client max connections: 100) preventing GPU out-of-memory crashes while allowing concurrent multi-agent speculative branches.
6. **Tier-0 Prometheus Metric Endpoint**:
   - Zero-dependency `GET /metrics` endpoint emitting Prometheus-compliant metrics for request latency, queue depth, priority states, and system thermals.

---

## 🚀 Quickstart

### 1. Installation
```bash
git clone https://github.com/Jaswanth1902/OS_Hammer.git
cd OS_Hammer
pip install -r requirements.txt
```

### 2. Launch Proxy
```bash
# Direct execution (listens on :11434, forwards to upstream :11435)
python main.py
```

### 3. Dry-Run Verification
```bash
python main.py --dry-run
```

---

## ⚙️ Configuration (`config.yaml`)

```yaml
host: "127.0.0.1"
port: 11434
upstream_url: "http://127.0.0.1:11435"

hammer:
  enabled: true
  poll_interval_sec: 0.5
  debounce_cycles: 2
  engage_delay_sec: 1.0
  release_delay_sec: 3.0

max_threads: 24
```

---

## 🧪 Testing

```bash
pytest tests/test_command_center.py -v
# 14 passed in 1.80s (100% coverage)
```

---

## 🏛️ License & Author

- **Author**: K. Sai Jaswanth Reddy ([@Jaswanth1902](https://github.com/Jaswanth1902))
- **License**: MIT License.
