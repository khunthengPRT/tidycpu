#!/usr/bin/env python3
"""
TidyCPU — CPU Affinity Optimization Utility
============================================
Balances CPU load by reassigning process affinity across cores.
Target: Linux x64 (Debian/Ubuntu)
Requires: root privileges
"""

import os
import sys
import re
import subprocess
import time
import argparse
from dataclasses import dataclass, field
from typing import Optional
from collections import defaultdict
from datetime import datetime

VERSION = "1.0"

# ─────────────────────────────────────────────
# ANSI Color Palette
# ─────────────────────────────────────────────
class C:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    RED     = "\033[91m"
    GREEN   = "\033[92m"
    YELLOW  = "\033[93m"
    BLUE    = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN    = "\033[96m"
    WHITE   = "\033[97m"
    BG_RED  = "\033[41m"
    BG_DARK = "\033[100m"

# ─────────────────────────────────────────────
# Data Structures
# ─────────────────────────────────────────────
@dataclass
class CPUTopology:
    """Physical CPU topology info."""
    physical_id:  int
    core_id:      int
    logical_id:   int
    is_hyperthread: bool = False

@dataclass
class SystemInfo:
    """System-wide hardware information."""
    cpu_model:     str
    total_memory:  str  # e.g., "15.6 GiB"
    available_memory: str
    kernel_cmdline: str
    cpu_freq_min:  Optional[float] = None  # MHz
    cpu_freq_max:  Optional[float] = None  # MHz
    cpu_freq_cur:  Optional[float] = None  # MHz

@dataclass
class CoreStat:
    core_id:  int
    usage:    float   # 0.0 – 100.0 percent
    label:    str     # HOT / WARM / COLD
    pids:     list    = field(default_factory=list)
    physical_id: Optional[int] = None
    core_within_physical: Optional[int] = None
    top_proc: str     = ""  # name of the busiest process/thread on this core

@dataclass
class ThreadInfo:
    """Individual thread details."""
    tid:          int
    tgid:         int  # parent process group id
    name:         str
    cpu_percent:  float
    current_cores: list[int]

@dataclass
class ProcessInfo:
    pid:          int
    name:         str
    cpu_percent:  float
    current_cores: list[int]
    affinity_mask: str
    threads:      list[ThreadInfo] = field(default_factory=list)

@dataclass
class RebalanceAction:
    pid:           int
    name:          str
    cpu_percent:   float
    from_cores:    list[int]
    to_cores:      list[int]
    manual_only:   bool = False
    error_msg:     str  = ""

@dataclass
class Snapshot:
    """Single snapshot of system state during live monitoring."""
    timestamp:    str
    iteration:    int
    core_stats:   list[CoreStat]
    processes:    list[ProcessInfo]

# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────
def die(msg: str):
    print(f"\n{C.RED}{C.BOLD}[FATAL]{C.RESET} {msg}\n")
    sys.exit(1)

def run(cmd: list[str], timeout: int = 5) -> tuple[int, str, str]:
    """Run a subprocess, return (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "Command timed out"
    except FileNotFoundError:
        return -1, "", f"Command not found: {cmd[0]}"

def parse_cpulist(cpulist_str: str) -> list[int]:
    """Parse kernel cpulist format '0,2-5,7' → [0, 2, 3, 4, 5, 7]."""
    cores = []
    for part in cpulist_str.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-")
            cores.extend(range(int(lo), int(hi) + 1))
        elif part.isdigit():
            cores.append(int(part))
    return sorted(set(cores))

def cores_to_cpulist(cores: list[int]) -> str:
    """[0, 2, 3, 4] → '0,2-4'."""
    if not cores:
        return ""
    cores = sorted(set(cores))
    parts, start, end = [], cores[0], cores[0]
    for c in cores[1:]:
        if c == end + 1:
            end = c
        else:
            parts.append(str(start) if start == end else f"{start}-{end}")
            start = end = c
    parts.append(str(start) if start == end else f"{start}-{end}")
    return ",".join(parts)

def mask_to_cores(hex_mask: str, num_cores: int) -> list[int]:
    """Convert hex affinity mask to list of core IDs."""
    try:
        mask = int(hex_mask.replace(",", ""), 16)
        return [i for i in range(num_cores) if mask & (1 << i)]
    except ValueError:
        return []

def resolve_pid(pid_or_name: str) -> int:
    """
    Resolve a single process name or PID string to an integer PID.
    If the value is numeric, return it directly.
    Otherwise, use pgrep to find the PID by process name.
    Returns the first matched PID, or exits with an error.
    """
    if pid_or_name.isdigit():
        return int(pid_or_name)

    # Name-based lookup via pgrep (exact match first)
    rc, out, _ = run(["pgrep", "-x", pid_or_name])
    if rc == 0 and out:
        pids = out.splitlines()
        if len(pids) > 1:
            print(f"  {C.YELLOW}⚠  Multiple processes match '{pid_or_name}': {', '.join(pids)}{C.RESET}")
            print(f"  {C.YELLOW}   Using PID {pids[0]}{C.RESET}")
        return int(pids[0])

    # Fallback: partial match
    rc2, out2, _ = run(["pgrep", pid_or_name])
    if rc2 == 0 and out2:
        pids = out2.splitlines()
        if len(pids) > 1:
            print(f"  {C.YELLOW}⚠  Multiple processes match '{pid_or_name}': {', '.join(pids)}{C.RESET}")
            print(f"  {C.YELLOW}   Using PID {pids[0]}{C.RESET}")
        return int(pids[0])

    die(f"No process found matching name '{pid_or_name}'.\n"
        f"  Try: pgrep {pid_or_name}  to verify the process exists.")

def resolve_pids(pid_spec: str) -> list[int]:
    """
    Resolve a pipe-separated list of PIDs/names to a deduplicated list of integer PIDs.
    Examples:
      "1234"           → [1234]
      "nginx"          → [<pid of nginx>]
      "nginx|php-fpm|1234" → [<nginx pid>, <php-fpm pid>, 1234]
    """
    tokens = [t.strip() for t in pid_spec.split("|") if t.strip()]
    seen: set[int] = set()
    result: list[int] = []
    for token in tokens:
        pid = resolve_pid(token)
        if pid not in seen:
            seen.add(pid)
            result.append(pid)
    return result

# ─────────────────────────────────────────────
# Step 0 – Privilege Check
# ─────────────────────────────────────────────
def check_root():
    if os.geteuid() != 0:
        die(
            "TidyCPU requires root privileges.\n"
            f"  {C.YELLOW}Re-run with: sudo python3 tidycpu.py{C.RESET}"
        )

# ─────────────────────────────────────────────
# System Information Collection
# ─────────────────────────────────────────────
def get_system_info(show_cpu_freq: bool = False) -> SystemInfo:
    """
    Collect system-wide hardware information.
    """
    # CPU Model from /proc/cpuinfo
    cpu_model = "Unknown"
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    cpu_model = line.split(":", 1)[1].strip()
                    break
    except FileNotFoundError:
        pass
    
    # Memory info from /proc/meminfo
    total_mem = "Unknown"
    avail_mem = "Unknown"
    try:
        with open("/proc/meminfo") as f:
            meminfo = {}
            for line in f:
                parts = line.split(":", 1)
                if len(parts) == 2:
                    key = parts[0].strip()
                    value = parts[1].strip().split()[0]  # Get numeric part
                    meminfo[key] = int(value)
            
            if "MemTotal" in meminfo:
                total_kb = meminfo["MemTotal"]
                total_mem = f"{total_kb / 1024 / 1024:.1f} GiB"
            
            if "MemAvailable" in meminfo:
                avail_kb = meminfo["MemAvailable"]
                avail_mem = f"{avail_kb / 1024 / 1024:.1f} GiB"
    except (FileNotFoundError, ValueError):
        pass
    
    # Kernel command line
    cmdline = "Unknown"
    try:
        with open("/proc/cmdline") as f:
            cmdline = f.read().strip()
    except FileNotFoundError:
        pass
    
    # CPU frequency info (optional)
    freq_min, freq_max, freq_cur = None, None, None
    if show_cpu_freq:
        try:
            # Try cpufreq interface for CPU0
            with open("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_min_freq") as f:
                freq_min = int(f.read().strip()) / 1000  # Convert kHz to MHz
            with open("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq") as f:
                freq_max = int(f.read().strip()) / 1000
            with open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq") as f:
                freq_cur = int(f.read().strip()) / 1000
        except (FileNotFoundError, ValueError, PermissionError):
            # Fallback: try to get from cpuinfo
            try:
                with open("/proc/cpuinfo") as f:
                    for line in f:
                        if line.startswith("cpu MHz"):
                            freq_cur = float(line.split(":", 1)[1].strip())
                            break
            except (FileNotFoundError, ValueError):
                pass
    
    return SystemInfo(
        cpu_model=cpu_model,
        total_memory=total_mem,
        available_memory=avail_mem,
        kernel_cmdline=cmdline,
        cpu_freq_min=freq_min,
        cpu_freq_max=freq_max,
        cpu_freq_cur=freq_cur
    )

# ─────────────────────────────────────────────
# CPU Topology Detection
# ─────────────────────────────────────────────
def get_cpu_topology() -> dict[int, CPUTopology]:
    """
    Parse /sys/devices/system/cpu/cpu*/topology to map logical cores
    to physical CPUs and physical cores.
    """
    topology = {}
    cpu_dirs = sorted(
        [d for d in os.listdir("/sys/devices/system/cpu") if d.startswith("cpu") and d[3:].isdigit()],
        key=lambda x: int(x[3:])
    )
    
    for cpu_dir in cpu_dirs:
        logical_id = int(cpu_dir[3:])
        base = f"/sys/devices/system/cpu/{cpu_dir}/topology"
        
        try:
            with open(f"{base}/physical_package_id") as f:
                physical_id = int(f.read().strip())
            with open(f"{base}/core_id") as f:
                core_id = int(f.read().strip())
            
            # Check if this is a hyperthread sibling
            with open(f"{base}/thread_siblings_list") as f:
                siblings = parse_cpulist(f.read().strip())
                is_hyperthread = len(siblings) > 1 and logical_id != min(siblings)
            
            topology[logical_id] = CPUTopology(
                physical_id=physical_id,
                core_id=core_id,
                logical_id=logical_id,
                is_hyperthread=is_hyperthread
            )
        except (FileNotFoundError, ValueError):
            # Fallback for systems without topology info
            topology[logical_id] = CPUTopology(
                physical_id=0,
                core_id=logical_id,
                logical_id=logical_id,
                is_hyperthread=False
            )
    
    return topology

# ─────────────────────────────────────────────
# Step 1 – Telemetry: Read /proc/stat (two snapshots)
# ─────────────────────────────────────────────
def read_proc_stat() -> dict[int, dict]:
    """Parse /proc/stat and return per-core raw counters."""
    cores = {}
    try:
        with open("/proc/stat") as f:
            for line in f:
                m = re.match(r"^cpu(\d+)\s+(.+)$", line)
                if m:
                    cid = int(m.group(1))
                    vals = list(map(int, m.group(2).split()))
                    # user nice system idle iowait irq softirq steal guest guest_nice
                    cores[cid] = {
                        "user":    vals[0], "nice":    vals[1],
                        "system":  vals[2], "idle":    vals[3],
                        "iowait":  vals[4] if len(vals) > 4 else 0,
                        "irq":     vals[5] if len(vals) > 5 else 0,
                        "softirq": vals[6] if len(vals) > 6 else 0,
                        "steal":   vals[7] if len(vals) > 7 else 0,
                    }
    except FileNotFoundError:
        die("/proc/stat not found. Are you on Linux?")
    return cores

def get_top_proc_per_core() -> dict[int, str]:
    """
    Read /proc/*/stat and /proc/*/task/*/stat to find the process or thread
    with the highest recent CPU time on each logical core.
    Returns a dict mapping core_id -> "name" of the busiest task.
    """
    # Maps core_id -> (max_cputime, name)
    best: dict[int, tuple[int, str]] = {}

    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except PermissionError:
        return {}

    for pid_str in pids:
        pid = int(pid_str)
        task_dir = f"/proc/{pid}/task"
        try:
            tids = [t for t in os.listdir(task_dir) if t.isdigit()]
        except (FileNotFoundError, PermissionError):
            continue

        for tid_str in tids:
            try:
                with open(f"{task_dir}/{tid_str}/stat") as f:
                    raw = f.read()
                # stat format: pid (comm) state ppid pgroup session tty_nr
                #   tpgid flags minflt cminflt majflt cmajflt
                #   utime stime cutime cstime priority nice ...
                #   (38) last_cpu
                # comm may contain spaces so we parse past the closing ')'
                rp = raw.rfind(")")
                if rp == -1:
                    continue
                fields = raw[rp + 2:].split()
                # fields[0] = state, fields[11]=utime, fields[12]=stime, fields[36]=processor
                if len(fields) < 37:
                    continue
                utime    = int(fields[11])
                stime    = int(fields[12])
                cpu_time = utime + stime
                last_cpu = int(fields[36])

                # Read thread comm for the name
                comm_path = f"{task_dir}/{tid_str}/comm"
                with open(comm_path) as f:
                    name = f.read().strip()[:15]

                cur_best = best.get(last_cpu)
                if cur_best is None or cpu_time > cur_best[0]:
                    best[last_cpu] = (cpu_time, name)
            except (FileNotFoundError, PermissionError, ValueError, IndexError):
                continue

    return {core: name for core, (_, name) in best.items()}


def get_core_usage(sample_ms: int = 500, topology: Optional[dict] = None) -> list[CoreStat]:
    """Two-snapshot delta to calculate real per-core CPU %."""
    snap1 = read_proc_stat()
    time.sleep(sample_ms / 1000)
    snap2 = read_proc_stat()

    stats = []
    for cid in sorted(snap1.keys()):
        s1, s2 = snap1[cid], snap2[cid]

        idle1  = s1["idle"] + s1["iowait"]
        idle2  = s2["idle"] + s2["iowait"]
        total1 = sum(s1.values())
        total2 = sum(s2.values())

        d_idle  = idle2  - idle1
        d_total = total2 - total1

        usage = 0.0 if d_total == 0 else (1.0 - d_idle / d_total) * 100.0

        if   usage >= 80: label = "HOT"
        elif usage >= 40: label = "WARM"
        else:             label = "COLD"

        phys_id = None
        core_in_phys = None
        if topology and cid in topology:
            phys_id = topology[cid].physical_id
            core_in_phys = topology[cid].core_id

        stats.append(CoreStat(
            core_id=cid,
            usage=round(usage, 1),
            label=label,
            physical_id=phys_id,
            core_within_physical=core_in_phys
        ))

    # Populate top_proc from /proc after both snapshots are done
    top_procs = get_top_proc_per_core()
    for cs in stats:
        cs.top_proc = top_procs.get(cs.core_id, "")

    return stats

# ─────────────────────────────────────────────
# Step 2 – Analysis: Top CPU Processes + Affinity
# ─────────────────────────────────────────────
def get_threads_for_pid(pid: int, num_cores: int = 1) -> list[ThreadInfo]:
    """Get all threads for a specific PID with their CPU usage."""
    threads = []
    try:
        # Read thread list from /proc/<pid>/task/
        task_dir = f"/proc/{pid}/task"
        if not os.path.exists(task_dir):
            return threads
        
        for tid_str in os.listdir(task_dir):
            if not tid_str.isdigit():
                continue
            tid = int(tid_str)
            
            try:
                # Get thread name
                with open(f"{task_dir}/{tid}/comm") as f:
                    name = f.read().strip()[:24]
                
                # Get thread CPU usage from /proc/<pid>/task/<tid>/stat
                with open(f"{task_dir}/{tid}/stat") as f:
                    stat = f.read().strip()
                    pass
                
                # Use ps to get thread-level CPU
                rc, out, _ = run(["ps", "-p", str(tid), "-o", "pcpu", "--no-headers"])
                cpu = 0.0
                if rc == 0 and out:
                    try:
                        cpu = float(out.strip())
                    except ValueError:
                        pass
                
                # Get thread affinity
                rc2, out2, _ = run(["taskset", "-p", str(tid)])
                cur_cores = []
                if rc2 == 0:
                    m = re.search(r"current affinity mask:\s*([0-9a-fA-F,]+)", out2)
                    if m:
                        cur_cores = mask_to_cores(m.group(1), num_cores)
                
                threads.append(ThreadInfo(
                    tid=tid,
                    tgid=pid,
                    name=name,
                    cpu_percent=cpu,
                    current_cores=cur_cores
                ))
            except (FileNotFoundError, PermissionError):
                continue
    except (FileNotFoundError, PermissionError):
        pass
    
    return threads

def get_top_processes(n: int = 5, num_cores: int = 1, with_threads: bool = False) -> list[ProcessInfo]:
    """Get top-N CPU-consuming processes with their core affinity."""
    rc, out, err = run([
        "ps", "ax", "-o", "pid,pcpu,comm", "--sort=-pcpu", "--no-headers"
    ])
    if rc != 0:
        die(f"ps failed: {err}")

    procs = []
    for line in out.splitlines()[:20]:
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid  = int(parts[0])
            cpu  = float(parts[1])
            name = parts[2].strip()[:24]
        except ValueError:
            continue

        # Get affinity mask via taskset
        rc2, out2, err2 = run(["taskset", "-p", str(pid)])
        if rc2 != 0:
            affinity_mask = "N/A"
            cur_cores     = []
        else:
            m = re.search(r"current affinity mask:\s*([0-9a-fA-F,]+)", out2)
            affinity_mask = m.group(1) if m else "N/A"
            cur_cores     = mask_to_cores(affinity_mask, num_cores) if m else []

        threads = []
        if with_threads:
            threads = get_threads_for_pid(pid, num_cores)

        procs.append(ProcessInfo(
            pid=pid, name=name, cpu_percent=cpu,
            current_cores=cur_cores, affinity_mask=affinity_mask,
            threads=threads
        ))
        if len(procs) == n:
            break

    return procs

# ─────────────────────────────────────────────
# Step 3 – Conflict Detection + Plan
# ─────────────────────────────────────────────
def build_rebalance_plan(
    core_stats: list[CoreStat],
    processes:  list[ProcessInfo],
) -> list[RebalanceAction]:
    """
    Flag PIDs crowding HOT cores while COLD cores are idle.
    Suggest moving them to COLD cores in round-robin.
    """
    hot_ids  = {c.core_id for c in core_stats if c.label == "HOT"}
    cold_ids = [c.core_id for c in core_stats if c.label == "COLD"]

    core_pid_map: dict[int, list] = {c.core_id: [] for c in core_stats}
    for proc in processes:
        for cid in proc.current_cores:
            if cid in core_pid_map:
                core_pid_map[cid].append(proc.pid)

    actions: list[RebalanceAction] = []
    cold_iter = iter(cold_ids * (len(processes) + 1))

    for proc in processes:
        on_hot  = [c for c in proc.current_cores if c in hot_ids]
        has_conflict = (
            len(on_hot) > 0 and
            len(cold_ids) > 0 and
            len(proc.current_cores) < len(core_stats)
        )

        if not has_conflict:
            continue

        try:
            to_cores = [next(cold_iter) for _ in proc.current_cores]
        except StopIteration:
            to_cores = cold_ids[:len(proc.current_cores)] or cold_ids[:1]

        actions.append(RebalanceAction(
            pid=proc.pid, name=proc.name,
            cpu_percent=proc.cpu_percent,
            from_cores=proc.current_cores,
            to_cores=to_cores,
        ))

    return actions

# ─────────────────────────────────────────────
# Step 4 – Execution
# ─────────────────────────────────────────────
def apply_action(action: RebalanceAction) -> RebalanceAction:
    """Apply taskset; mark as manual_only on failure."""
    cpu_list = cores_to_cpulist(action.to_cores)
    rc, out, err = run(["taskset", "-pc", cpu_list, str(action.pid)])
    if rc != 0:
        action.manual_only = True
        action.error_msg   = err or out or "Permission denied / kernel thread"
    return action

# ─────────────────────────────────────────────
# UI – Pretty Printers
# ─────────────────────────────────────────────
BANNER = f"""
{C.CYAN}{C.BOLD}╔══════════════════════════════════════════════════════════╗
║          TidyCPU  —  CPU Affinity Optimizer              ║
║          Linux x64  |  Requires root                     ║
╚══════════════════════════════════════════════════════════╝{C.RESET}"""

def usage_bar(pct: float, width: int = 20) -> str:
    filled = int(pct / 100 * width)
    bar    = "█" * filled + "░" * (width - filled)
    if   pct >= 80: color = C.RED
    elif pct >= 40: color = C.YELLOW
    else:           color = C.GREEN
    return f"{color}{bar}{C.RESET}"

def label_color(label: str) -> str:
    return {
        "HOT":  f"{C.RED}{C.BOLD}HOT {C.RESET}",
        "WARM": f"{C.YELLOW}WARM{C.RESET}",
        "COLD": f"{C.CYAN}COLD{C.RESET}",
    }.get(label, label)

def print_system_info(sysinfo: SystemInfo):
    """Print system hardware information."""
    print(f"\n{C.BOLD}{'─'*78}{C.RESET}")
    print(f"{C.BOLD}  SYSTEM INFORMATION{C.RESET}")
    print(f"{C.BOLD}{'─'*78}{C.RESET}")
    
    print(f"  {C.CYAN}CPU Model:{C.RESET}")
    print(f"    {sysinfo.cpu_model}")
    
    print(f"\n  {C.CYAN}Memory:{C.RESET}")
    print(f"    Total:     {C.GREEN}{sysinfo.total_memory}{C.RESET}")
    print(f"    Available: {C.GREEN}{sysinfo.available_memory}{C.RESET}")
    
    if sysinfo.cpu_freq_cur is not None:
        print(f"\n  {C.CYAN}CPU Frequency:{C.RESET}")
        if sysinfo.cpu_freq_min and sysinfo.cpu_freq_max:
            print(f"    Range:   {sysinfo.cpu_freq_min:.0f} MHz - {sysinfo.cpu_freq_max:.0f} MHz")
        print(f"    Current: {C.YELLOW}{sysinfo.cpu_freq_cur:.0f} MHz{C.RESET}")
    
    print(f"\n  {C.CYAN}Kernel Command Line:{C.RESET}")
    cmdline = sysinfo.kernel_cmdline
    if len(cmdline) > 72:
        words = cmdline.split()
        lines = []
        current_line = "    "
        for word in words:
            if len(current_line) + len(word) + 1 > 78:
                lines.append(current_line)
                current_line = "    " + word
            else:
                current_line += (" " if len(current_line) > 4 else "") + word
        if current_line.strip():
            lines.append(current_line)
        for line in lines:
            print(f"{C.DIM}{line}{C.RESET}")
    else:
        print(f"    {C.DIM}{cmdline}{C.RESET}")

def print_topology(topology: dict[int, CPUTopology], core_stats: list[CoreStat]):
    """Print CPU topology in compact two-column layout."""
    print(f"\n{C.BOLD}{'─'*78}{C.RESET}")
    print(f"{C.BOLD}  CPU TOPOLOGY{C.RESET}")
    print(f"{C.BOLD}{'─'*78}{C.RESET}")
    
    stats_map = {cs.core_id: cs for cs in core_stats}
    total_cores = len(topology)
    
    left_count = (total_cores + 1) // 2
    
    print(f"\n  {'Core':<6} {'Usage':>6}  {'Bar':<22}  {'Status':<6}  {'Process':<16}  "
          f"{'Core':<6} {'Usage':>6}  {'Bar':<22}  {'Status':<6}  {'Process':<16}")
    print(f"  {'────':<6} {'─────':>6}  {'──────────────────────':<22}  {'──────':<6}  {'────────────────':<16}  "
          f"{'────':<6} {'─────':>6}  {'──────────────────────':<22}  {'──────':<6}  {'────────────────':<16}")
    
    for i in range(left_count):
        left_id = i
        right_id = i + left_count
        
        if left_id in stats_map:
            cs_left = stats_map[left_id]
            core_name_left = f"CPU{cs_left.core_id}"
            proc_left = (cs_left.top_proc[:15] if cs_left.top_proc else C.DIM + '—' + C.RESET)
            left_line = (
                f"  {C.DIM}{core_name_left:<6}{C.RESET} "
                f"{cs_left.usage:>5.1f}%  "
                f"{usage_bar(cs_left.usage, width=22)}  "
                f"{label_color(cs_left.label):<6}  "
                f"{C.MAGENTA}{proc_left:<16}{C.RESET}"
            )
        else:
            left_line = " " * 68
        
        if right_id < total_cores and right_id in stats_map:
            cs_right = stats_map[right_id]
            core_name_right = f"CPU{cs_right.core_id}"
            proc_right = (cs_right.top_proc[:15] if cs_right.top_proc else C.DIM + '—' + C.RESET)
            right_line = (
                f"  {C.DIM}{core_name_right:<6}{C.RESET} "
                f"{cs_right.usage:>5.1f}%  "
                f"{usage_bar(cs_right.usage, width=22)}  "
                f"{label_color(cs_right.label):<6}  "
                f"{C.MAGENTA}{proc_right:<16}{C.RESET}"
            )
        else:
            right_line = ""
        
        print(left_line + right_line)
    
    hot  = sum(1 for c in core_stats if c.label == "HOT")
    cold = sum(1 for c in core_stats if c.label == "COLD")
    warm = sum(1 for c in core_stats if c.label == "WARM")
    
    by_physical = {}
    for logical_id, topo in topology.items():
        if topo.physical_id not in by_physical:
            by_physical[topo.physical_id] = []
        by_physical[topo.physical_id].append(topo)
    
    total_physical = len(by_physical)
    total_physical_cores = sum(
        len(set(t.core_id for t in cores)) for cores in by_physical.values()
    )
    ht_enabled = total_cores > total_physical_cores
    
    print(f"\n  Summary: {C.RED}●{C.RESET} {hot} Hot  "
          f"{C.YELLOW}●{C.RESET} {warm} Warm  "
          f"{C.CYAN}●{C.RESET} {cold} Cold  {C.DIM}|{C.RESET}  "
          f"{total_physical} physical CPU(s), "
          f"{total_physical_cores} physical core(s), "
          f"{total_cores} logical core(s)")
    
    if ht_enabled:
        print(f"  Hyperthreading: {C.GREEN}Enabled{C.RESET}")
    else:
        print(f"  Hyperthreading: {C.DIM}Disabled{C.RESET}")

def clear_screen():
    """Clear terminal screen."""
    os.system('clear' if os.name != 'nt' else 'cls')

def _fetch_process_info(pid: int, num_cores: int) -> Optional[ProcessInfo]:
    """Fetch a single ProcessInfo for the given PID. Returns None if unavailable."""
    rc, out, _ = run(["ps", "-p", str(pid), "-o", "pid,pcpu,comm", "--no-headers"])
    if rc != 0 or not out:
        return None
    parts = out.strip().split(None, 2)
    if len(parts) < 3:
        return None
    try:
        pid_val = int(parts[0])
        cpu     = float(parts[1])
        name    = parts[2].strip()[:24]
    except ValueError:
        return None

    rc2, out2, _ = run(["taskset", "-p", str(pid_val)])
    affinity_mask = "N/A"
    cur_cores: list[int] = []
    if rc2 == 0:
        m = re.search(r"current affinity mask:\s*([0-9a-fA-F,]+)", out2)
        if m:
            affinity_mask = m.group(1)
            cur_cores = mask_to_cores(affinity_mask, num_cores)

    threads = get_threads_for_pid(pid_val, num_cores)
    return ProcessInfo(
        pid=pid_val, name=name, cpu_percent=cpu,
        current_cores=cur_cores, affinity_mask=affinity_mask,
        threads=threads
    )

def live_monitor(duration_sec: int = 5, interval_ms: int = 3000, filter_pids: Optional[list[int]] = None, show_cpu_freq: bool = False, export_html: Optional[str] = None, export_text: Optional[str] = None, sysinfo: Optional[SystemInfo] = None, topology_data: Optional[dict] = None):
    """
    Live monitoring mode - refresh stats every interval_ms for duration_sec iterations.
    CPU is sampled for SAMPLE_MS (fast), then the screen is shown for the remaining
    display time so the user can actually read it before the next refresh.
    If filter_pids is set, show threads for each of those PIDs.
    Collects snapshots for export if export options are provided.
    """
    SAMPLE_MS = 500   # CPU sampling window — short, accurate
    display_ms = max(interval_ms - SAMPLE_MS, 1000)  # how long screen stays visible

    topology = topology_data if topology_data else get_cpu_topology()
    num_cores = len(topology)

    iterations = duration_sec
    snapshots = [] if (export_html or export_text) else None

    for i in range(iterations):
        # ── 1. Sample CPU (happens invisibly, screen still showing previous frame) ──
        core_stats = get_core_usage(sample_ms=SAMPLE_MS, topology=topology)

        # ── 2. Collect process info ───────────────────────────────────────────────
        processes: list[ProcessInfo] = []
        if filter_pids:
            for pid in filter_pids:
                info = _fetch_process_info(pid, num_cores)
                if info is not None:
                    processes.append(info)

        # ── 3. Render (clear → print → user reads) ────────────────────────────────
        clear_screen()
        print(BANNER)

        if i == 0 and sysinfo:
            print_system_info(sysinfo)

        refresh_label = f"{interval_ms // 1000}s"
        print(f"\n  {C.CYAN}Live Monitor Mode{C.RESET} — {C.DIM}Refresh: {refresh_label}  "
              f"Iteration: {i+1}/{iterations}{C.RESET}")

        print_topology(topology, core_stats)

        if filter_pids:
            label = " | ".join(str(p) for p in filter_pids)
            print(f"\n  {C.YELLOW}Filtering: {label}{C.RESET}")
            for info in processes:
                print_process_details_live(info)
            not_found = [p for p in filter_pids if not any(pr.pid == p for pr in processes)]
            for pid in not_found:
                print(f"  {C.RED}✘ PID {pid} not found or inaccessible{C.RESET}")

        if snapshots is not None:
            snapshot = Snapshot(
                timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                iteration=i + 1,
                core_stats=core_stats,
                processes=processes,
            )
            snapshots.append(snapshot)

        print(f"\n  {C.DIM}Press Ctrl+C to exit{C.RESET}")

        # ── 4. Sleep the display window so the user can read the screen ───────────
        if i < iterations - 1:
            time.sleep(display_ms / 1000)
    
    print(f"\n  {C.GREEN}Live monitoring complete.{C.RESET}\n")
    
    if snapshots and sysinfo:
        if export_html:
            try:
                filename = export_to_html(sysinfo, topology, core_stats, [], export_html, snapshots=snapshots)
                print(f"  {C.GREEN}✔{C.RESET}  HTML report with {len(snapshots)} snapshots exported to: {C.CYAN}{filename}{C.RESET}\n")
            except Exception as e:
                print(f"  {C.RED}✘{C.RESET}  HTML export failed: {e}\n")
        
        if export_text:
            try:
                filename = export_to_text(sysinfo, topology, core_stats, [], export_text, snapshots=snapshots)
                print(f"  {C.GREEN}✔{C.RESET}  Text report with {len(snapshots)} snapshots exported to: {C.CYAN}{filename}{C.RESET}\n")
            except Exception as e:
                print(f"  {C.RED}✘{C.RESET}  Text export failed: {e}\n")

def print_process_details_live(proc: ProcessInfo):
    """Print a single process's thread details during live monitoring (no affinity mask)."""
    print(f"\n{C.BOLD}{'─'*62}{C.RESET}")
    print(f"{C.BOLD}  PROCESS: {proc.name}  (PID {proc.pid})  —  {C.YELLOW}{proc.cpu_percent:.1f}% CPU{C.RESET}")
    print(f"{C.BOLD}{'─'*62}{C.RESET}")

    if proc.threads:
        print(f"  {'TID':>7}  {'Name':<24}  {'CPU%':>6}  Cores")
        print(f"  {'───':>7}  {'────────────────────────':<24}  {'─────':>6}  ─────")
        for t in proc.threads[:15]:
            t_cores_str = ",".join(map(str, t.current_cores)) if t.current_cores else "all"
            print(
                f"  {t.tid:>7}  {t.name:<24}  "
                f"{C.YELLOW}{t.cpu_percent:>5.1f}%{C.RESET}  {t_cores_str}"
            )
        if len(proc.threads) > 15:
            print(f"  {C.DIM}... {len(proc.threads) - 15} more threads{C.RESET}")

def print_rebalance_plan(actions: list[RebalanceAction]):
    print(f"\n{C.BOLD}{'─'*62}{C.RESET}")
    print(f"{C.BOLD}  REBALANCING PLAN{C.RESET}")
    print(f"{C.BOLD}{'─'*62}{C.RESET}")
    if not actions:
        print(f"  {C.GREEN}✔  No conflicts detected. CPU load looks balanced!{C.RESET}")
        return

    print(f"  {'PID':>7}  {'Process':<20}  {'CPU%':>5}  {'From → To'}")
    print(f"  {'───────':>7}  {'────────────────────':20}  {'─────':>5}  {'──────────────────────'}")
    for a in actions:
        from_s = ",".join(map(str, a.from_cores))
        to_s   = ",".join(map(str, a.to_cores))
        arrow  = (
            f"{C.RED}Core [{from_s}]{C.RESET}"
            f" {C.DIM}→{C.RESET} "
            f"{C.GREEN}Core [{to_s}]{C.RESET}"
        )
        print(
            f"  {C.MAGENTA}{a.pid:>7}{C.RESET}  "
            f"{a.name:<20}  "
            f"{C.YELLOW}{a.cpu_percent:>4.1f}%{C.RESET}  "
            f"{arrow}"
        )
    print(f"\n  {len(actions)} change(s) proposed.")

def print_results(actions: list[RebalanceAction]):
    print(f"\n{C.BOLD}{'─'*62}{C.RESET}")
    print(f"{C.BOLD}  EXECUTION RESULTS{C.RESET}")
    print(f"{C.BOLD}{'─'*62}{C.RESET}")
    ok      = [a for a in actions if not a.manual_only]
    manual  = [a for a in actions if a.manual_only]

    for a in ok:
        to_s = ",".join(map(str, a.to_cores))
        print(f"  {C.GREEN}✔{C.RESET}  PID {C.MAGENTA}{a.pid}{C.RESET} ({a.name})  →  pinned to core(s) [{to_s}]")

    if manual:
        print(f"\n  {C.YELLOW}⚠  Manual Suggestions (could not auto-apply):{C.RESET}")
        for a in manual:
            to_s = ",".join(map(str, a.to_cores))
            print(
                f"  {C.YELLOW}↪{C.RESET}  PID {C.MAGENTA}{a.pid}{C.RESET} ({a.name})"
                f"  — run manually:\n"
                f"       {C.DIM}sudo taskset -pc {to_s} {a.pid}{C.RESET}\n"
                f"       {C.RED}Reason: {a.error_msg}{C.RESET}"
            )

# ─────────────────────────────────────────────
# Export Functions
# ─────────────────────────────────────────────
def export_to_html(
    sysinfo: SystemInfo,
    topology: dict[int, CPUTopology],
    core_stats: list[CoreStat],
    processes: list[ProcessInfo],
    filename: str = "tidycpu_report.html",
    snapshots: Optional[list[Snapshot]] = None
):
    """Export current state to HTML file with styling."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>TidyCPU Report - {timestamp}</title>
    <style>
        body {{
            font-family: 'Consolas', 'Monaco', monospace;
            background: #1e1e1e;
            color: #d4d4d4;
            padding: 20px;
            line-height: 1.6;
        }}
        .container {{ max-width: 1400px; margin: 0 auto; }}
        h1 {{ color: #4fc3f7; border-bottom: 2px solid #4fc3f7; padding-bottom: 10px; }}
        h2 {{ color: #81c784; margin-top: 30px; }}
        h3 {{ color: #ffb74d; margin-top: 20px; }}
        .section {{
            background: #252526; padding: 20px; margin: 20px 0;
            border-radius: 5px; border-left: 4px solid #4fc3f7;
        }}
        .snapshot {{
            background: #2d2d30; padding: 15px; margin: 15px 0;
            border-radius: 5px; border-left: 4px solid #ffb74d;
        }}
        .info-grid {{ display: grid; grid-template-columns: 200px 1fr; gap: 10px; margin: 10px 0; }}
        .label {{ color: #4fc3f7; font-weight: bold; }}
        table {{ width: 100%; border-collapse: collapse; margin: 15px 0; font-size: 14px; }}
        th {{ background: #2d2d30; color: #4fc3f7; padding: 10px; text-align: left; border-bottom: 2px solid #4fc3f7; }}
        td {{ padding: 8px 10px; border-bottom: 1px solid #3e3e42; }}
        tr:hover {{ background: #2d2d30; }}
        .bar {{ background: #3e3e42; height: 20px; border-radius: 3px; overflow: hidden; }}
        .bar-fill {{ height: 100%; }}
        .bar-fill.hot {{ background: #f44336; }}
        .bar-fill.warm {{ background: #ff9800; }}
        .bar-fill.cold {{ background: #4caf50; }}
        .status {{ padding: 3px 8px; border-radius: 3px; font-weight: bold; font-size: 12px; }}
        .status.hot {{ background: #f44336; color: white; }}
        .status.warm {{ background: #ff9800; color: white; }}
        .status.cold {{ background: #4caf50; color: white; }}
        .summary {{ display: flex; gap: 20px; margin: 15px 0; flex-wrap: wrap; }}
        .summary-item {{ background: #2d2d30; padding: 10px 15px; border-radius: 5px; }}
        .dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 5px; }}
        .dot.hot {{ background: #f44336; }}
        .dot.warm {{ background: #ff9800; }}
        .dot.cold {{ background: #4caf50; }}
        .timestamp {{ color: #858585; font-size: 12px; }}
        .two-column {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }}
        .iteration-badge {{
            display: inline-block; background: #ffb74d; color: #1e1e1e;
            padding: 5px 10px; border-radius: 5px; font-weight: bold; margin-left: 10px;
        }}
        .process-name {{ color: #ce9178; font-style: italic; }}
        .tabs {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 16px; }}
        .tab-btn {{
            background: #2d2d30; color: #9cdcfe; border: 1px solid #3e3e42;
            padding: 6px 14px; border-radius: 4px; cursor: pointer; font-family: inherit;
            font-size: 13px; transition: background 0.15s;
        }}
        .tab-btn:hover {{ background: #3e3e42; }}
        .tab-btn.active {{ background: #ffb74d; color: #1e1e1e; border-color: #ffb74d; font-weight: bold; }}
        .tab-panel {{ display: none; }}
        .tab-panel.active {{ display: block; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>TidyCPU — CPU Affinity Optimization Report</h1>
        <p class="timestamp">Generated: {timestamp}</p>
"""
    
    # System Information
    html += """
        <div class="section">
            <h2>System Information</h2>
            <div class="info-grid">
                <div class="label">CPU Model:</div>
                <div>{}</div>
                <div class="label">Total Memory:</div>
                <div>{}</div>
                <div class="label">Available Memory:</div>
                <div>{}</div>
""".format(sysinfo.cpu_model, sysinfo.total_memory, sysinfo.available_memory)
    
    if sysinfo.cpu_freq_cur:
        html += f"""
                <div class="label">CPU Frequency:</div>
                <div>{sysinfo.cpu_freq_min:.0f} MHz - {sysinfo.cpu_freq_max:.0f} MHz (Current: {sysinfo.cpu_freq_cur:.0f} MHz)</div>
"""
    
    html += f"""
                <div class="label">Kernel Cmdline:</div>
                <div style="word-break: break-all;">{sysinfo.kernel_cmdline}</div>
            </div>
        </div>
"""
    
    def render_topology_section(core_stats_data, title_suffix=""):
        stats_map = {cs.core_id: cs for cs in core_stats_data}
        total_cores = len(stats_map)
        left_count = (total_cores + 1) // 2
        
        hot_count  = sum(1 for c in core_stats_data if c.label == "HOT")
        warm_count = sum(1 for c in core_stats_data if c.label == "WARM")
        cold_count = sum(1 for c in core_stats_data if c.label == "COLD")
        
        by_physical = {}
        for logical_id, topo in topology.items():
            if topo.physical_id not in by_physical:
                by_physical[topo.physical_id] = []
            by_physical[topo.physical_id].append(topo)
        
        total_physical = len(by_physical)
        total_physical_cores = sum(len(set(t.core_id for t in cores)) for cores in by_physical.values())
        ht_enabled = total_cores > total_physical_cores
        
        s = f"""
            <h3>CPU Topology{title_suffix}</h3>
            <div class="summary">
                <div class="summary-item"><span class="dot hot"></span>{hot_count} Hot</div>
                <div class="summary-item"><span class="dot warm"></span>{warm_count} Warm</div>
                <div class="summary-item"><span class="dot cold"></span>{cold_count} Cold</div>
                <div class="summary-item">{total_physical} Physical CPU(s)</div>
                <div class="summary-item">{total_physical_cores} Physical Cores</div>
                <div class="summary-item">{total_cores} Logical Cores</div>
                <div class="summary-item">HT: {'Enabled' if ht_enabled else 'Disabled'}</div>
            </div>
            <div class="two-column">
"""
        
        for col_start, col_end in [(0, left_count), (left_count, total_cores)]:
            s += """
                <table>
                    <thead><tr><th>Core</th><th>Usage</th><th>Bar</th><th>Status</th><th>Process</th></tr></thead>
                    <tbody>
"""
            for i in range(col_start, col_end):
                if i in stats_map:
                    cs = stats_map[i]
                    lc = cs.label.lower()
                    s += f"""
                        <tr>
                            <td>CPU{cs.core_id}</td>
                            <td>{cs.usage:.1f}%</td>
                            <td><div class="bar"><div class="bar-fill {lc}" style="width:{cs.usage}%"></div></div></td>
                            <td><span class="status {lc}">{cs.label}</span></td>
                            <td><span class="process-name">{cs.top_proc or '&#8212;'}</span></td>
                        </tr>
"""
            s += "                    </tbody>\n                </table>\n"
        
        s += "            </div>\n"
        return s
    
    if snapshots:
        html += f"""
        <div class="section">
            <h2>Live Monitoring Results <span class="iteration-badge">{len(snapshots)} Snapshots</span></h2>
            <div class="tabs">
"""
        for snap in snapshots:
            active = 'active' if snap.iteration == 1 else ''
            ts = snap.timestamp.split()[1]
            html += f'                <button class="tab-btn {active}" onclick="showTab({snap.iteration})" id="btn-{snap.iteration}">#{snap.iteration} &nbsp;<span style="color:#858585;font-size:11px">{ts}</span></button>\n'
        html += "            </div>\n"
        for snap in snapshots:
            active = 'active' if snap.iteration == 1 else ''
            html += f"""
            <div class="tab-panel {active}" id="panel-{snap.iteration}">
                {render_topology_section(snap.core_stats)}
            </div>
"""
        html += """
<script>
            function showTab(n) {{
                document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
                document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
                document.getElementById('panel-' + n).classList.add('active');
                document.getElementById('btn-' + n).classList.add('active');
            }}
            </script>
        </div>
"""
    else:
        html += f"""
        <div class="section">
            {render_topology_section(core_stats)}
        </div>
"""
    
    html += """
    </div>
</body>
</html>
"""
    
    with open(filename, 'w') as f:
        f.write(html)
    
    return filename

def export_to_text(
    sysinfo: SystemInfo,
    topology: dict[int, CPUTopology],
    core_stats: list[CoreStat],
    processes: list[ProcessInfo],
    filename: str = "tidycpu_report.txt",
    snapshots: Optional[list[Snapshot]] = None
):
    """Export current state to text file."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    output = []
    output.append("=" * 78)
    output.append("TidyCPU — CPU Affinity Optimization Report")
    output.append(f"Generated: {timestamp}")
    output.append("=" * 78)
    output.append("")
    
    output.append("SYSTEM INFORMATION")
    output.append("-" * 78)
    output.append(f"CPU Model:        {sysinfo.cpu_model}")
    output.append(f"Total Memory:     {sysinfo.total_memory}")
    output.append(f"Available Memory: {sysinfo.available_memory}")
    
    if sysinfo.cpu_freq_cur:
        output.append(f"CPU Frequency:    {sysinfo.cpu_freq_min:.0f} MHz - {sysinfo.cpu_freq_max:.0f} MHz")
        output.append(f"                  (Current: {sysinfo.cpu_freq_cur:.0f} MHz)")
    
    output.append(f"Kernel Cmdline:   {sysinfo.kernel_cmdline}")
    output.append("")
    
    def render_topology(core_stats_data, title_suffix=""):
        stats_map = {cs.core_id: cs for cs in core_stats_data}
        total_cores = len(stats_map)
        left_count = (total_cores + 1) // 2
        
        hot_count  = sum(1 for c in core_stats_data if c.label == "HOT")
        warm_count = sum(1 for c in core_stats_data if c.label == "WARM")
        cold_count = sum(1 for c in core_stats_data if c.label == "COLD")
        
        by_physical = {}
        for logical_id, topo in topology.items():
            if topo.physical_id not in by_physical:
                by_physical[topo.physical_id] = []
            by_physical[topo.physical_id].append(topo)
        
        total_physical = len(by_physical)
        total_physical_cores = sum(len(set(t.core_id for t in cores)) for cores in by_physical.values())
        ht_enabled = total_cores > total_physical_cores
        
        lines = []
        lines.append(f"CPU TOPOLOGY{title_suffix}")
        lines.append("-" * 78)
        lines.append(f"  {'Core':<6} {'Usage':>6}  {'Bar':<22}  {'Status'}")
        lines.append(f"  {'────':<6} {'─────':>6}  {'──────────────────────':<22}  {'──────'}")
        
        for i in range(total_cores):
            if i in stats_map:
                cs = stats_map[i]
                bar = '█' * int(cs.usage / 100 * 22) + '░' * (22 - int(cs.usage / 100 * 22))
                lines.append(f"  CPU{cs.core_id:<3} {cs.usage:>5.1f}%  {bar}  {cs.label}")
        
        lines.append("")
        lines.append(f"Summary: ● {hot_count} Hot  ● {warm_count} Warm  ● {cold_count} Cold  |  "
                     f"{total_physical} physical CPU(s), {total_physical_cores} physical core(s), "
                     f"{total_cores} logical core(s)")
        lines.append(f"Hyperthreading: {'Enabled' if ht_enabled else 'Disabled'}")
        return lines
    
    if snapshots:
        output.append(f"LIVE MONITORING RESULTS ({len(snapshots)} snapshots)")
        output.append("=" * 78)
        output.append("")
        
        for snap in snapshots:
            output.append(f"[Iteration {snap.iteration}] @ {snap.timestamp}")
            output.append("-" * 78)
            output.extend(render_topology(snap.core_stats, f" - Iteration {snap.iteration}"))
            output.append("")
            output.append("=" * 78)
            output.append("")
    else:
        output.extend(render_topology(core_stats))
        output.append("")
    
    output.append("=" * 78)
    
    with open(filename, 'w') as f:
        f.write('\n'.join(output))
    
    return filename

# ─────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="TidyCPU — CPU Affinity Optimization Utility",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  sudo tidycpu                                  # View CPU topology only
  sudo tidycpu --threads                        # Show processes and rebalancing
  sudo tidycpu --check-pid 1234                 # Check specific process affinity
  sudo tidycpu --live                           # Live monitor (5 seconds)
  sudo tidycpu --live --duration 10             # Live monitor (10 seconds)
  sudo tidycpu --pid 1234                       # Monitor single PID
  sudo tidycpu --pid nginx                      # Monitor process by name
  sudo tidycpu --pid "nginx|php-fpm"            # Monitor multiple processes
  sudo tidycpu --pid "1234|nginx|5678"          # Mix of PIDs and names
  sudo tidycpu --cpu-freq                       # Include CPU frequency info
  sudo tidycpu --export-html report.html        # Export to HTML
  sudo tidycpu --version                        # Show version
        """
    )
    parser.add_argument("--live", "-l", action="store_true",
        help="Live monitoring mode (refreshes every 500ms)")
    parser.add_argument("--duration", "-d", type=int, default=5,
        help="Duration for live mode in seconds (default: 5)")
    parser.add_argument("--pid", "-p", type=str,
        help="PIDs/names to monitor, pipe-separated (e.g. nginx|php-fpm|1234)")
    parser.add_argument("--threads", "-t", action="store_true",
        help="Show thread information for processes")
    parser.add_argument("--cpu-freq", "-f", action="store_true",
        help="Show CPU frequency information (min/max/current)")
    parser.add_argument("--check-pid", type=int, metavar="PID",
        help="Check affinity and CPU usage for a specific process ID")
    parser.add_argument("--export-html", type=str, metavar="FILE",
        help="Export report to HTML file (e.g., report.html)")
    parser.add_argument("--export-text", type=str, metavar="FILE",
        help="Export report to text file (e.g., report.txt)")
    parser.add_argument("--version", "-v", action="version",
        version=f"tidycpu {VERSION}")

    args = parser.parse_args()
    
    print(BANNER)

    check_root()
    print(f"  {C.GREEN}✔{C.RESET}  Running as root.\n")

    sysinfo = get_system_info(show_cpu_freq=args.cpu_freq)
    print_system_info(sysinfo)

    topology = get_cpu_topology()
    num_cores = len(topology)

    # Resolve --pid (pipe-separated names/PIDs) early so all branches can use it
    resolved_pids: list[int] = []
    if args.pid:
        resolved_pids = resolve_pids(args.pid)

    # ── Check specific PID mode ──────────────────────────────────────────────
    if args.check_pid:
        pid = args.check_pid
        print(f"\n  {C.CYAN}Checking PID {pid}{C.RESET}\n")
        
        rc, out, _ = run(["ps", "-p", str(pid), "-o", "pid,pcpu,comm", "--no-headers"])
        if rc != 0:
            print(f"  {C.RED}✘ PID {pid} not found{C.RESET}\n")
            sys.exit(1)
        
        parts = out.strip().split(None, 2)
        if len(parts) < 3:
            print(f"  {C.RED}✘ Unable to read process info{C.RESET}\n")
            sys.exit(1)
        
        try:
            pid_val = int(parts[0])
            cpu_percent = float(parts[1])
            name = parts[2].strip()
        except ValueError:
            print(f"  {C.RED}✘ Invalid process data{C.RESET}\n")
            sys.exit(1)
        
        rc2, out2, _ = run(["taskset", "-p", str(pid_val)])
        if rc2 != 0:
            print(f"  {C.RED}✘ Unable to read affinity{C.RESET}\n")
            sys.exit(1)
        
        m = re.search(r"current affinity mask:\s*([0-9a-fA-F,]+)", out2)
        if not m:
            print(f"  {C.RED}✘ Unable to parse affinity mask{C.RESET}\n")
            sys.exit(1)
        
        current_cores = mask_to_cores(m.group(1), num_cores)
        core_stats = get_core_usage(sample_ms=500, topology=topology)
        stats_map = {cs.core_id: cs for cs in core_stats}
        
        print(f"{C.BOLD}{'─'*78}{C.RESET}")
        print(f"{C.BOLD}  PROCESS DETAILS{C.RESET}")
        print(f"{C.BOLD}{'─'*78}{C.RESET}")
        print(f"  {C.CYAN}PID:{C.RESET}      {pid_val}")
        print(f"  {C.CYAN}Process:{C.RESET}  {name}")
        print(f"  {C.CYAN}CPU:%{C.RESET}     {C.YELLOW}{cpu_percent:.1f}%{C.RESET}")
        
        cores_str = ",".join(map(str, current_cores)) if current_cores else "all"
        print(f"  {C.CYAN}Cores:{C.RESET}    {cores_str}")
        
        print(f"\n{C.BOLD}  CORE STATUS{C.RESET}")
        print(f"{C.BOLD}{'─'*78}{C.RESET}")
        
        if current_cores:
            for core_id in current_cores:
                if core_id in stats_map:
                    cs = stats_map[core_id]
                    bar = usage_bar(cs.usage, width=20)
                    print(f"  CPU{core_id:>2}  {cs.usage:>5.1f}%  {bar}  {label_color(cs.label)}")
        else:
            print(f"  {C.DIM}Process can run on any core (no affinity set){C.RESET}")
        
        threads = get_threads_for_pid(pid_val, num_cores)
        if threads:
            print(f"\n{C.BOLD}  THREADS ({len(threads)}){C.RESET}")
            print(f"{C.BOLD}{'─'*78}{C.RESET}")
            print(f"  {'TID':>7}  {'Name':<24}  {'CPU%':>6}  Cores")
            print(f"  {'───':>7}  {'────────────────────────':<24}  {'─────':>6}  ─────")
            
            for t in threads[:15]:
                t_cores_str = ",".join(map(str, t.current_cores)) if t.current_cores else "all"
                print(f"  {t.tid:>7}  {t.name:<24}  {C.YELLOW}{t.cpu_percent:>5.1f}%{C.RESET}  {t_cores_str}")
            
            if len(threads) > 15:
                print(f"  {C.DIM}... {len(threads) - 15} more threads{C.RESET}")
        
        print()
        sys.exit(0)

    # ── Live monitor / PID-filter mode ──────────────────────────────────────
    if args.live or resolved_pids:
        try:
            live_monitor(
                duration_sec=args.duration,
                interval_ms=3000,
                filter_pids=resolved_pids or None,
                show_cpu_freq=args.cpu_freq,
                export_html=args.export_html,
                export_text=args.export_text,
                sysinfo=sysinfo,
                topology_data=topology
            )
        except KeyboardInterrupt:
            print(f"\n\n  {C.YELLOW}Monitoring interrupted.{C.RESET}\n")
        return

    # ── Standard rebalance mode ──────────────────────────────────────────────
    print(f"\n  {C.CYAN}○{C.RESET}  Sampling core usage (500 ms) …", end="", flush=True)
    core_stats = get_core_usage(sample_ms=500, topology=topology)
    print(f"\r  {C.GREEN}✔{C.RESET}  Core telemetry collected.          ")

    print_topology(topology, core_stats)

    processes = []

    if args.threads:
        print(f"\n  {C.CYAN}○{C.RESET}  Identifying top CPU consumers …", end="", flush=True)
        processes = get_top_processes(n=5, num_cores=num_cores, with_threads=True)
        print(f"\r  {C.GREEN}✔{C.RESET}  Process snapshot ready.            ")

    if args.export_html:
        try:
            if not processes:
                processes = get_top_processes(n=5, num_cores=num_cores, with_threads=False)
            filename = export_to_html(sysinfo, topology, core_stats, processes, args.export_html)
            print(f"\n  {C.GREEN}✔{C.RESET}  HTML report exported to: {C.CYAN}{filename}{C.RESET}")
        except Exception as e:
            print(f"\n  {C.RED}✘{C.RESET}  HTML export failed: {e}")
    
    if args.export_text:
        try:
            if not processes:
                processes = get_top_processes(n=5, num_cores=num_cores, with_threads=False)
            filename = export_to_text(sysinfo, topology, core_stats, processes, args.export_text)
            print(f"\n  {C.GREEN}✔{C.RESET}  Text report exported to: {C.CYAN}{filename}{C.RESET}")
        except Exception as e:
            print(f"\n  {C.RED}✘{C.RESET}  Text export failed: {e}")

    if args.threads:
        actions = build_rebalance_plan(core_stats, processes)
        print_rebalance_plan(actions)

        if not actions:
            print(f"\n{C.DIM}  Nothing to do. Exiting.{C.RESET}\n")
            sys.exit(0)

        print(f"\n{C.BOLD}  Apply these changes? (y/n): {C.RESET}", end="", flush=True)
        try:
            answer = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{C.YELLOW}  Aborted.{C.RESET}\n")
            sys.exit(0)

        if answer != "y":
            print(f"  {C.YELLOW}⚡  No changes applied. Plan saved for manual review.{C.RESET}\n")
            for a in actions:
                to_s = cores_to_cpulist(a.to_cores)
                print(f"     {C.DIM}sudo taskset -pc {to_s} {a.pid}{C.RESET}")
            print()
            sys.exit(0)

        print(f"\n  {C.CYAN}○{C.RESET}  Applying affinity changes …\n")
        results = [apply_action(a) for a in actions]
        print_results(results)
        print(f"\n{C.GREEN}{C.BOLD}  TidyCPU complete.{C.RESET}\n")
    else:
        print(f"\n{C.DIM}  Use --threads to see processes and rebalancing options.{C.RESET}\n")

if __name__ == "__main__":
    main()