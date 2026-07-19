"""Resource samplers for benchmark runs (ctypes, shell-free RSS probe).

Samples the working set of every process whose executable name contains
a target substring (e.g. "ollama" catches the server and its runner
subprocesses), plus system available RAM; optional VRAM sampling via
bounded nvidia-smi calls at a coarser interval.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import subprocess
import threading
import time
from collections.abc import Callable

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MAX_RECORDED_SYSTEM_SAMPLES = 256


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_uint64),
        ("ullAvailPhys", ctypes.c_uint64),
        ("ullTotalPageFile", ctypes.c_uint64),
        ("ullAvailPageFile", ctypes.c_uint64),
        ("ullTotalVirtual", ctypes.c_uint64),
        ("ullAvailVirtual", ctypes.c_uint64),
        ("ullAvailExtendedVirtual", ctypes.c_uint64),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


def process_tree_metrics(exe_substring: str) -> tuple[int, int | None]:
    """Return aggregate working set and cumulative disk-read bytes.

    The I/O counter is process cumulative rather than device-wide. It is
    therefore useful as bounded evidence of runtime disk traffic without
    claiming that unrelated system I/O belongs to the model.
    """
    kernel32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    pids = (wintypes.DWORD * 8192)()
    returned = wintypes.DWORD(0)
    if not psapi.EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(returned)):
        return 0, None
    count = returned.value // ctypes.sizeof(wintypes.DWORD)
    needle = exe_substring.lower()
    total = 0
    read_bytes = 0
    io_available = False
    for pid in pids[:count]:
        if not pid:
            continue
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            continue
        try:
            name_buffer = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, name_buffer, ctypes.byref(size)):
                continue
            exe = name_buffer.value.rsplit("\\", 1)[-1].lower()
            if needle not in exe:
                continue
            counters = _PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(counters)
            if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                total += int(counters.WorkingSetSize)
            io_counters = _IO_COUNTERS()
            if kernel32.GetProcessIoCounters(handle, ctypes.byref(io_counters)):
                read_bytes += int(io_counters.ReadTransferCount)
                io_available = True
        finally:
            kernel32.CloseHandle(handle)
    return total, read_bytes if io_available else None


def process_tree_working_set_bytes(exe_substring: str) -> int:
    """Sum the working sets of all processes whose exe name matches."""
    return process_tree_metrics(exe_substring)[0]


def system_memory_status() -> dict[str, int] | None:
    status = _MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return {
        "total_ram_bytes": int(status.ullTotalPhys),
        "available_ram_bytes": int(status.ullAvailPhys),
        "memory_load_percent": int(status.dwMemoryLoad),
        "pagefile_total_bytes": int(status.ullTotalPageFile),
        "pagefile_available_bytes": int(status.ullAvailPageFile),
    }


def system_available_ram_bytes() -> int:
    status = system_memory_status()
    return int(status["available_ram_bytes"]) if status else 0


def vram_used_bytes(smi_path: str) -> int | None:
    try:
        proc = subprocess.run(  # noqa: S603 - argv array, bounded
            [smi_path, "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=4,
            shell=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    try:
        first = (proc.stdout or "").splitlines()[0].strip()
        return int(float(first)) * 1024 * 1024
    except (IndexError, ValueError):
        return None


class ResourceSampler:
    """Background peak-tracking sampler for one benchmark run."""

    def __init__(
        self,
        *,
        exe_substring: str,
        interval_ms: int = 250,
        smi_path: str | None = None,
        vram_interval_ms: int = 1000,
        safety_floor_bytes: int | None = None,
        on_safety_floor: Callable[[], None] | None = None,
    ):
        self.exe_substring = exe_substring
        self.interval_ms = interval_ms
        self.smi_path = smi_path
        self.vram_interval_ms = vram_interval_ms
        self.safety_floor_bytes = safety_floor_bytes
        self.on_safety_floor = on_safety_floor
        self.peak_rss_bytes = 0
        self.min_available_ram_bytes: int | None = None
        self.vram_peak_used_bytes: int | None = None
        self.samples = 0
        self.system_memory_samples: list[dict[str, int]] = []
        self.sampling_available = True
        self.sampling_failure_category = ""
        self.safety_floor_crossed = False
        self.disk_read_start_bytes: int | None = None
        self.disk_read_end_bytes: int | None = None
        self.pagefile_used_peak_bytes: int | None = None
        self.started_at = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _loop(self) -> None:
        last_vram = 0.0
        self.started_at = time.monotonic()
        while not self._stop.is_set():
            rss, disk_read = process_tree_metrics(self.exe_substring)
            if rss > self.peak_rss_bytes:
                self.peak_rss_bytes = rss
            if disk_read is not None:
                if self.disk_read_start_bytes is None:
                    self.disk_read_start_bytes = disk_read
                self.disk_read_end_bytes = disk_read
            status = system_memory_status()
            available = int(status["available_ram_bytes"]) if status else 0
            if status is None:
                self.sampling_available = False
                self.sampling_failure_category = "memory_probe_unavailable"
            if available and (
                self.min_available_ram_bytes is None or available < self.min_available_ram_bytes
            ):
                self.min_available_ram_bytes = available
            if status and len(self.system_memory_samples) < MAX_RECORDED_SYSTEM_SAMPLES:
                pagefile_used = max(
                    0,
                    int(status["pagefile_total_bytes"])
                    - int(status["pagefile_available_bytes"]),
                )
                self.system_memory_samples.append(
                    {
                        "elapsed_ms": int((time.monotonic() - self.started_at) * 1000),
                        "available_ram_bytes": available,
                        "memory_load_percent": int(status["memory_load_percent"]),
                        "pagefile_used_bytes": pagefile_used,
                    }
                )
            elif status:
                pagefile_used = max(
                    0,
                    int(status["pagefile_total_bytes"])
                    - int(status["pagefile_available_bytes"]),
                )
            else:
                pagefile_used = None
            if pagefile_used is not None and (
                self.pagefile_used_peak_bytes is None or pagefile_used > self.pagefile_used_peak_bytes
            ):
                self.pagefile_used_peak_bytes = pagefile_used
            if (
                available
                and self.safety_floor_bytes is not None
                and available < self.safety_floor_bytes
                and not self.safety_floor_crossed
            ):
                self.safety_floor_crossed = True
                if self.on_safety_floor is not None:
                    try:
                        self.on_safety_floor()
                    except Exception:  # noqa: BLE001 - monitoring must remain bounded
                        pass
            now = time.monotonic()
            if self.smi_path and (now - last_vram) * 1000 >= self.vram_interval_ms:
                last_vram = now
                used = vram_used_bytes(self.smi_path)
                if used is not None and (
                    self.vram_peak_used_bytes is None or used > self.vram_peak_used_bytes
                ):
                    self.vram_peak_used_bytes = used
            self.samples += 1
            self._stop.wait(self.interval_ms / 1000)

    def __enter__(self) -> "ResourceSampler":
        self._thread = threading.Thread(target=self._loop, name="bench-sampler", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def to_dict(self) -> dict:
        return {
            "runtime_peak_rss_bytes": self.peak_rss_bytes,
            "system_min_available_bytes": self.min_available_ram_bytes or 0,
            "vram_peak_used_bytes": self.vram_peak_used_bytes,
            "sampler_interval_ms": self.interval_ms,
            "samples": self.samples,
        }

    def to_v2_dict(self) -> dict:
        """Closed-schema interference observations for paired artifacts."""
        disk_read_bytes = None
        if self.disk_read_start_bytes is not None and self.disk_read_end_bytes is not None:
            disk_read_bytes = max(0, self.disk_read_end_bytes - self.disk_read_start_bytes)
        return {
            **self.to_dict(),
            "system_memory_samples": list(self.system_memory_samples),
            "sampling_available": self.sampling_available,
            "sampling_failure_category": self.sampling_failure_category,
            "disk_read_bytes": disk_read_bytes,
            "pagefile_used_peak_bytes": self.pagefile_used_peak_bytes,
            "safety_floor_crossed": self.safety_floor_crossed,
        }
