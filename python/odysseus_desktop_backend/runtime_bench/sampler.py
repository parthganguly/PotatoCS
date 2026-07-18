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

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


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


def process_tree_working_set_bytes(exe_substring: str) -> int:
    """Sum the working sets of all processes whose exe name matches."""
    kernel32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    pids = (wintypes.DWORD * 8192)()
    returned = wintypes.DWORD(0)
    if not psapi.EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(returned)):
        return 0
    count = returned.value // ctypes.sizeof(wintypes.DWORD)
    needle = exe_substring.lower()
    total = 0
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
        finally:
            kernel32.CloseHandle(handle)
    return total


def system_available_ram_bytes() -> int:
    status = _MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return 0
    return int(status.ullAvailPhys)


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
    ):
        self.exe_substring = exe_substring
        self.interval_ms = interval_ms
        self.smi_path = smi_path
        self.vram_interval_ms = vram_interval_ms
        self.peak_rss_bytes = 0
        self.min_available_ram_bytes: int | None = None
        self.vram_peak_used_bytes: int | None = None
        self.samples = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _loop(self) -> None:
        last_vram = 0.0
        while not self._stop.is_set():
            rss = process_tree_working_set_bytes(self.exe_substring)
            if rss > self.peak_rss_bytes:
                self.peak_rss_bytes = rss
            available = system_available_ram_bytes()
            if available and (
                self.min_available_ram_bytes is None or available < self.min_available_ram_bytes
            ):
                self.min_available_ram_bytes = available
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
