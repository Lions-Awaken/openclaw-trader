"""Hardware collectors for system_monitor.py on Jetson Orin Nano.

Minimal implementation using psutil + /sys reads + tegrastats. Replaces the
original systems-console/collectors.py that was never committed to the repo.

All functions tolerate missing sensors and return sensible defaults — the daemon
must never crash because a single sysfs path is unreadable.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from typing import Optional

try:
    import psutil  # type: ignore
except ImportError:
    psutil = None  # type: ignore


# ── CPU ───────────────────────────────────────────────────────────────────────


def get_cpu_utilization() -> tuple[float, list[float]]:
    """Return (total_pct, per_core_pct_list). 500ms sampling window."""
    if psutil is None:
        return 0.0, []
    cores = psutil.cpu_percent(interval=0.5, percpu=True)
    total = sum(cores) / max(len(cores), 1)
    return total, cores


def get_cpu_freq_mhz() -> int:
    """Current CPU clock (cpu0 scaling_cur_freq in kHz → MHz)."""
    try:
        with open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq") as f:
            return int(f.read().strip()) // 1000
    except Exception:
        return 0


# ── Memory ────────────────────────────────────────────────────────────────────


def get_mem_stats() -> dict:
    """Return /proc/meminfo dict with values in KB. Keys: MemTotal, MemAvailable, ..."""
    result: dict[str, int] = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                val = rest.strip().split()[0] if rest.strip() else ""
                try:
                    result[key] = int(val)
                except ValueError:
                    continue
    except Exception:
        pass
    return result


def get_process_rss_mb(name_substring: str) -> float:
    """RSS in MB for the first process whose name contains the substring."""
    if psutil is None:
        return 0.0
    needle = name_substring.lower()
    try:
        for p in psutil.process_iter(["name", "memory_info"]):
            try:
                if needle in (p.info.get("name") or "").lower():
                    return p.info["memory_info"].rss / (1024 * 1024)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        pass
    return 0.0


# ── GPU ───────────────────────────────────────────────────────────────────────


def _read_first(paths: list[str]) -> Optional[str]:
    for path in paths:
        try:
            with open(path) as f:
                return f.read().strip()
        except Exception:
            continue
    return None


def get_gpu_load() -> tuple[float, int]:
    """GPU load percentage (0-100) and current frequency (MHz).

    Jetson sysfs /sys/devices/gpu.0/load returns an integer 0-1000 representing
    0-100.0%. Frequency comes from the gv11b devfreq node (Hz → MHz).
    """
    pct = 0.0
    freq_mhz = 0
    raw_load = _read_first([
        "/sys/devices/gpu.0/load",
        "/sys/devices/platform/gpu.0/load",
        "/sys/devices/platform/17000000.gv11b/load",
    ])
    if raw_load is not None:
        try:
            pct = int(raw_load) / 10.0
        except ValueError:
            pass
    raw_freq = _read_first([
        "/sys/devices/gpu.0/devfreq/17000000.gv11b/cur_freq",
        "/sys/class/devfreq/17000000.gv11b/cur_freq",
    ])
    if raw_freq is not None:
        try:
            freq_mhz = int(raw_freq) // 1_000_000
        except ValueError:
            pass
    return pct, freq_mhz


# ── Thermal ───────────────────────────────────────────────────────────────────


def get_thermal_zones() -> dict:
    """Return temps (°C) per thermal zone. Maps common Jetson zone types to
    canonical 'cpu'/'gpu' keys for the dashboard; also keeps raw zone_type keys.
    """
    zones: dict[str, float] = {}
    base = "/sys/class/thermal"
    try:
        for name in os.listdir(base):
            if not name.startswith("thermal_zone"):
                continue
            zdir = f"{base}/{name}"
            try:
                with open(f"{zdir}/type") as f:
                    ztype = f.read().strip()
                with open(f"{zdir}/temp") as f:
                    # reported in millicelsius
                    temp_c = int(f.read().strip()) / 1000.0
            except Exception:
                continue
            zones[ztype] = temp_c
            ztype_lower = ztype.lower()
            if "cpu" in ztype_lower and "cpu" not in zones:
                zones["cpu"] = temp_c
            if "gpu" in ztype_lower and "gpu" not in zones:
                zones["gpu"] = temp_c
            if ("tj" in ztype_lower or "junction" in ztype_lower) and "tj" not in zones:
                zones["tj"] = temp_c
    except Exception:
        pass
    return zones


# ── Disk ──────────────────────────────────────────────────────────────────────


def get_disk_usage(path: str) -> tuple[float, float, float]:
    """Return (used_pct, used_gb, total_gb) for given mount path."""
    if psutil is None:
        return 0.0, 0.0, 0.0
    try:
        du = psutil.disk_usage(path)
        return du.percent, du.used / (1024**3), du.total / (1024**3)
    except Exception:
        return 0.0, 0.0, 0.0


# ── Power rails (via tegrastats) ──────────────────────────────────────────────


def _read_one_tegrastats_line(timeout_s: float = 2.0) -> str:
    """Spawn tegrastats, read one output line, terminate. Empty string on failure."""
    try:
        proc = subprocess.Popen(
            ["tegrastats", "--interval", "1000"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except Exception:
        return ""
    try:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            line = proc.stdout.readline() if proc.stdout else ""
            if line.strip():
                return line.strip()
        return ""
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=0.5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


_RAIL_PATTERNS = [
    ("VDD_IN", "vdd_in"),
    ("VDD_CPU_GPU_CV", "vdd_cpu_gpu_cv"),
    ("VDD_SOC", "vdd_soc"),
]


def get_power_rails() -> dict:
    """Parse tegrastats output for power rails (mW). Returns {} if unavailable."""
    line = _read_one_tegrastats_line()
    if not line:
        return {}
    rails: dict[str, float] = {}
    for rail_name, key in _RAIL_PATTERNS:
        match = re.search(rf"{rail_name}\s+(\d+)mW", line)
        if match:
            rails[key] = float(match.group(1))
    return rails


# ── Uptime ────────────────────────────────────────────────────────────────────


def get_uptime_seconds() -> int:
    """System uptime in seconds (from /proc/uptime)."""
    try:
        with open("/proc/uptime") as f:
            return int(float(f.read().split()[0]))
    except Exception:
        return 0
