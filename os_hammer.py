"""
Dynamic Preemptive OS Hammer (OS-Level Governor).
Monitors the foreground window on Windows using win32gui and psutil.
When a configured heavy tool (IDE, browser, creative app) gains focus, drops target LLM
processes (e.g., llama-server.exe) to IDLE_PRIORITY_CLASS. Restores to NORMAL_PRIORITY_CLASS
when focus is surrendered.
Dispatches silent push notifications exclusively through Ntfy.
"""

import sys
import time
import asyncio
import threading
from typing import Optional, List, Set, Dict, Tuple
import httpx
import psutil

from config import settings
from logger import telemetry

# Windows API bindings with graceful fallbacks
HAS_WIN32 = False
if sys.platform == "win32":
    try:
        import win32gui
        import win32process
        HAS_WIN32 = True
    except ImportError:
        HAS_WIN32 = False

# Windows Priority Class mappings
PRIORITY_CLASSES: Dict[str, int] = {
    "IDLE": getattr(psutil, "IDLE_PRIORITY_CLASS", 64),
    "BELOW_NORMAL": getattr(psutil, "BELOW_NORMAL_PRIORITY_CLASS", 16384),
    "NORMAL": getattr(psutil, "NORMAL_PRIORITY_CLASS", 32),
    "ABOVE_NORMAL": getattr(psutil, "ABOVE_NORMAL_PRIORITY_CLASS", 32768),
    "HIGH": getattr(psutil, "HIGH_PRIORITY_CLASS", 128),
}

IDLE_PRIORITY = PRIORITY_CLASSES["IDLE"]
BELOW_NORMAL_PRIORITY = PRIORITY_CLASSES["BELOW_NORMAL"]
NORMAL_PRIORITY = PRIORITY_CLASSES["NORMAL"]


def get_configured_priority(priority_name: str, default: int) -> Tuple[int, str]:
    """Resolves string priority name to psutil integer priority constant."""
    name = (priority_name or "").upper().strip()
    return PRIORITY_CLASSES.get(name, default), name


def get_foreground_process_name() -> Optional[str]:
    """Retrieves the executable filename of the current foreground window."""
    if not HAS_WIN32:
        return None
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return None
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid <= 0:
            return None
        proc = psutil.Process(pid)
        return proc.name().lower()
    except Exception:
        return None


def get_cpu_package_metrics() -> Tuple[Optional[float], float]:
    """
    Retrieves (cpu_temp_c, cpu_percent).
    Gracefully attempts WMI thermal zone query if accessible, falling back to None.
    Retrieves non-blocking cpu_percent via psutil.
    """
    try:
        cpu_percent = psutil.cpu_percent(interval=None)
    except Exception:
        cpu_percent = 0.0

    cpu_temp = None
    if HAS_WIN32:
        try:
            import win32com.client
            wmi_obj = win32com.client.GetObject("winmgmts:\\\\.\\root\\wmi")
            zones = wmi_obj.ExecQuery("SELECT CurrentTemperature FROM MSAcpi_ThermalZoneTemperature")
            for zone in zones:
                deg_c = (zone.CurrentTemperature - 2732) / 10.0
                if deg_c > 0:
                    cpu_temp = deg_c
                    break
        except Exception:
            cpu_temp = None
    return cpu_temp, cpu_percent


def fire_silent_ntfy(
    title: str, message: str, tags: str = "hammer,robot", priority: str = "min"
) -> None:
    """Dispatches a silent push notification in background thread (Zero-GUI, non-blocking)."""
    if not settings.ntfy_enabled or not settings.ntfy_url:
        return

    def _worker():
        try:
            headers = {
                "Title": title,
                "Priority": priority,  # 'min' ensures silent delivery with zero sound or alert dialog
                "Tags": tags,
            }
            timeout_sec = getattr(settings, "ntfy_timeout", 2.0)
            with httpx.Client(timeout=timeout_sec) as client:
                client.post(
                    settings.ntfy_url, content=message.encode("utf-8"), headers=headers
                )
        except Exception:
            # Silent fallback: never block or crash
            pass

    threading.Thread(target=_worker, name="NtfyPushThread", daemon=True).start()


class OSHammerGovernor:
    """Active governor tracking foreground window focus and adjusting process priorities."""

    def __init__(self):
        self.is_hammer_active: bool = False
        self.current_foreground_app: Optional[str] = None
        self.current_cpu_load: float = 0.0
        self.current_cpu_temp: Optional[float] = None
        self.current_throttle_reason: Optional[str] = None
        self.total_engagements: int = 0
        self._consecutive_heavy_hits: int = 0
        self._consecutive_light_hits: int = 0
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None

        # O(1) PID caching state
        self._cached_targets: List[psutil.Process] = []
        self._last_pid_scan_time: float = 0.0

        # Schmitt-trigger hysteresis state
        self.pending_state: Optional[bool] = None
        self.pending_since: Optional[float] = None
        self.last_transition_time: float = 0.0

    def find_target_processes(self, force_refresh: bool = False) -> List[psutil.Process]:
        """Discovers running processes matching configured target names with O(1) cache."""
        now = time.monotonic()
        target_names = set(settings.target_processes)

        # 1. Fast O(1) path: Validate existing cached process handles
        if (
            not force_refresh
            and self._cached_targets
            and (now - self._last_pid_scan_time < settings.hammer_pid_cache_ttl_sec)
        ):
            valid_targets = []
            for proc in self._cached_targets:
                try:
                    if proc.is_running() and proc.name().lower() in target_names:
                        valid_targets.append(proc)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            if valid_targets:
                self._cached_targets = valid_targets
                return self._cached_targets

        # 2. Slow fallback path: Full scan across OS process table
        targets: List[psutil.Process] = []
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                pname = proc.info["name"]
                if pname and pname.lower() in target_names:
                    targets.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        self._cached_targets = targets
        self._last_pid_scan_time = now
        return targets

    def apply_priority(self, priority: int, priority_name: str) -> int:
        """Applies process priority to all matching target LLM processes."""
        adjusted_count = 0
        targets = self.find_target_processes()

        for proc in targets:
            try:
                current_nice = proc.nice()
                if current_nice != priority:
                    proc.nice(priority)
                    adjusted_count += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied, Exception) as err:
                telemetry.console_logger.warning(
                    f"Could not adjust priority for PID {proc.pid}: {err}"
                )
        return adjusted_count

    def trigger_engage(self, foreground_exe: str) -> None:
        """Throttles target LLM runtimes to configured throttle priority (default BELOW_NORMAL) and alerts via Ntfy."""
        self.is_hammer_active = True
        self.total_engagements += 1
        self.last_transition_time = time.monotonic()
        target_prio, prio_name = get_configured_priority(
            getattr(settings, "hammer_throttle_priority", "BELOW_NORMAL"),
            BELOW_NORMAL_PRIORITY,
        )
        adjusted = self.apply_priority(target_prio, prio_name)

        telemetry.log_event(
            event_type="hammer_engaged",
            message=f"OS Hammer Engaged: '{foreground_exe}' active in foreground. Throttled {adjusted} processes to {prio_name} priority.",
            foreground_app=foreground_exe,
            hammer_active=True,
            extra={"adjusted_processes": adjusted, "target_priority": prio_name},
        )

        # Silent push notification
        fire_silent_ntfy(
            title=f"[OS Hammer] Prioritized: {foreground_exe}",
            message=f"Inference process adjusted to {prio_name} priority to preserve foreground responsiveness for {foreground_exe}.",
            tags="hammer,desktop",
            priority=settings.ntfy_silent_priority,
        )

    def trigger_disengage(self, foreground_exe: Optional[str]) -> None:
        """Restores target LLM runtimes to configured normal priority (default NORMAL)."""
        self.is_hammer_active = False
        self.last_transition_time = time.monotonic()
        target_prio, prio_name = get_configured_priority(
            getattr(settings, "hammer_normal_priority", "NORMAL"),
            NORMAL_PRIORITY,
        )
        adjusted = self.apply_priority(target_prio, prio_name)

        telemetry.log_event(
            event_type="hammer_restored",
            message=f"OS Hammer Disengaged: Heavy tool lost focus. Restored {adjusted} processes to {prio_name} priority.",
            foreground_app=foreground_exe,
            hammer_active=False,
            extra={"adjusted_processes": adjusted, "target_priority": prio_name},
        )

        # Silent push notification
        fire_silent_ntfy(
            title="[OS Hammer] Restored: Normal Priority",
            message="Heavy application surrendered focus. Background LLM inference restored to NORMAL priority.",
            tags="white_check_mark,robot",
            priority=settings.ntfy_silent_priority,
        )

    def check_cycle(self, now: Optional[float] = None) -> None:
        """
        Single check cycle evaluating foreground process (including GPU/creative/CAD/rendering tools)
        and CPU package temperature/load strain using Schmitt-trigger hysteresis and cycle debouncing.
        """
        if now is None:
            now = time.monotonic()

        fg_name = get_foreground_process_name()
        self.current_foreground_app = fg_name

        cpu_temp, cpu_load = get_cpu_package_metrics()
        self.current_cpu_temp = cpu_temp
        self.current_cpu_load = cpu_load

        heavy_set = set(settings.heavy_apps)
        is_heavy_fg = bool(fg_name and fg_name in heavy_set)

        # CPU Package load / temperature threshold check
        is_thermal_strain = False
        if getattr(settings, "hammer_check_cpu_package", True):
            if cpu_temp is not None and cpu_temp >= getattr(settings, "hammer_cpu_temp_threshold", 82.0):
                is_thermal_strain = True
            elif cpu_load >= getattr(settings, "hammer_cpu_load_threshold", 96.0):
                # Distinguish legitimate LLM inference workload from external system strain
                target_cpu_sum = 0.0
                try:
                    for p in self.find_target_processes():
                        target_cpu_sum += p.cpu_percent(interval=None)
                except Exception:
                    pass
                non_target_load = max(0.0, cpu_load - target_cpu_sum)
                if non_target_load >= getattr(settings, "hammer_cpu_load_threshold", 96.0) or cpu_load >= 98.5:
                    is_thermal_strain = True

        should_throttle = is_heavy_fg or is_thermal_strain

        if should_throttle:
            self._consecutive_heavy_hits += 1
            self._consecutive_light_hits = 0

            reason = fg_name if is_heavy_fg else (
                f"cpu_temp_{cpu_temp:.1f}C"
                if cpu_temp and cpu_temp >= getattr(settings, "hammer_cpu_temp_threshold", 80.0)
                else f"cpu_load_{cpu_load:.1f}%"
            )
            self.current_throttle_reason = reason

            if not self.is_hammer_active:
                if self.pending_state is not True:
                    self.pending_state = True
                    self.pending_since = now

                elapsed = now - self.pending_since
                if (
                    self._consecutive_heavy_hits >= settings.hammer_debounce_cycles
                    and elapsed >= settings.hammer_engage_delay_sec
                ):
                    self.trigger_engage(reason or "heavy_workload")
                    self.pending_state = None
                    self.pending_since = None
            else:
                self.pending_state = None
                self.pending_since = None
        else:
            self._consecutive_light_hits += 1
            self._consecutive_heavy_hits = 0
            self.current_throttle_reason = None

            if self.is_hammer_active:
                if self.pending_state is not False:
                    self.pending_state = False
                    self.pending_since = now

                elapsed = now - self.pending_since
                if (
                    self._consecutive_light_hits >= settings.hammer_debounce_cycles
                    and elapsed >= settings.hammer_release_delay_sec
                ):
                    self.trigger_disengage(fg_name)
                    self.pending_state = None
                    self.pending_since = None
            else:
                self.pending_state = None
                self.pending_since = None

    def _run_loop(self) -> None:
        """Internal background polling thread."""
        telemetry.console_logger.info(
            f"OS Hammer Governor initialized. Monitoring {len(settings.heavy_apps)} heavy apps at {settings.hammer_poll_interval}s interval."
        )
        while not self._stop_event.is_set():
            try:
                self.check_cycle()
            except Exception as e:
                telemetry.console_logger.error(f"Error in OS Hammer cycle: {e}")
            self._stop_event.wait(timeout=settings.hammer_poll_interval)

    def start(self) -> None:
        """Starts the background monitoring thread."""
        if not settings.hammer_enabled:
            telemetry.console_logger.info("OS Hammer is disabled in configuration.")
            return

        if self._worker_thread is not None and self._worker_thread.is_alive():
            return

        self._stop_event.clear()
        self._worker_thread = threading.Thread(
            target=self._run_loop, name="OSHammerGovernorThread", daemon=True
        )
        self._worker_thread.start()

    def stop(self) -> None:
        """Stops the monitoring thread and ensures priority is safely restored."""
        if self._worker_thread is not None and self._worker_thread.is_alive():
            self._stop_event.set()
            self._worker_thread.join(timeout=2.0)
            if self.is_hammer_active:
                self.apply_priority(NORMAL_PRIORITY, "NORMAL")
            self.is_hammer_active = False


# Global governor instance
governor = OSHammerGovernor()
