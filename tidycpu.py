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
                    # stat format: pid (comm) state ... utime stime ...
                    # We'll use ps for simplicity instead
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
    # Use ps for a reliable snapshot (RSS fields)
    rc, out, err = run([
        "ps", "ax", "-o", "pid,pcpu,comm", "--sort=-pcpu", "--no-headers"
    ])
    if rc != 0:
        die(f"ps failed: {err}")

    procs = []
    for line in out.splitlines()[:20]:      # sample top-20, filter below
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

    # Map core → list of heavy pids pinned there
    core_pid_map: dict[int, list] = {c.core_id: [] for c in core_stats}
    for proc in processes:
        for cid in proc.current_cores:
            if cid in core_pid_map:
                core_pid_map[cid].append(proc.pid)

    actions: list[RebalanceAction] = []
    cold_iter = iter(cold_ids * (len(processes) + 1))   # cycle through colds

    for proc in processes:
        on_hot  = [c for c in proc.current_cores if c in hot_ids]
        has_conflict = (
            len(on_hot) > 0 and
            len(cold_ids) > 0 and
            len(proc.current_cores) < len(core_stats)   # already spread wide? skip
        )

        if not has_conflict:
            continue

        # Pick next cold core(s) — same count as current assignment
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
    
    # CPU Model
    print(f"  {C.CYAN}CPU Model:{C.RESET}")
    print(f"    {sysinfo.cpu_model}")
    
    # Memory
    print(f"\n  {C.CYAN}Memory:{C.RESET}")
    print(f"    Total:     {C.GREEN}{sysinfo.total_memory}{C.RESET}")
    print(f"    Available: {C.GREEN}{sysinfo.available_memory}{C.RESET}")
    
    # CPU Frequency (if available)
    if sysinfo.cpu_freq_cur is not None:
        print(f"\n  {C.CYAN}CPU Frequency:{C.RESET}")
        if sysinfo.cpu_freq_min and sysinfo.cpu_freq_max:
            print(f"    Range:   {sysinfo.cpu_freq_min:.0f} MHz - {sysinfo.cpu_freq_max:.0f} MHz")
        print(f"    Current: {C.YELLOW}{sysinfo.cpu_freq_cur:.0f} MHz{C.RESET}")
    
    # Kernel Command Line
    print(f"\n  {C.CYAN}Kernel Command Line:{C.RESET}")
    # Wrap long cmdline for readability
    cmdline = sysinfo.kernel_cmdline
    if len(cmdline) > 72:
        # Split by spaces and wrap
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
    """Print CPU topology showing physical CPUs and their logical cores."""
    print(f"\n{C.BOLD}{'─'*78}{C.RESET}")
    print(f"{C.BOLD}  CPU TOPOLOGY{C.RESET}")
    print(f"{C.BOLD}{'─'*78}{C.RESET}")
    
    # Group by physical CPU
    by_physical: dict[int, list] = defaultdict(list)
    for logical_id, topo in sorted(topology.items()):
        by_physical[topo.physical_id].append((logical_id, topo))
    
    # Create a lookup for core stats
    stats_map = {cs.core_id: cs for cs in core_stats}
    
    for phys_id in sorted(by_physical.keys()):
        cores = by_physical[phys_id]
        print(f"\n  {C.MAGENTA}Physical CPU {phys_id}{C.RESET} ({len(cores)} logical cores)")
        
        # Group by physical core_id to show hyperthreads together
        by_core: dict[int, list] = defaultdict(list)
        for logical_id, topo in cores:
            by_core[topo.core_id].append((logical_id, topo))
        
        for core_id in sorted(by_core.keys()):
            logical_cores = by_core[core_id]
            
            if len(logical_cores) == 1:
                # No hyperthreading
                logical_id, topo = logical_cores[0]
                stat = stats_map.get(logical_id)
                usage = stat.usage if stat else 0.0
                label = stat.label if stat else "N/A"
                bar = usage_bar(usage, width=15)
                
                print(f"    ├─ Core {core_id}: {C.CYAN}CPU{logical_id:>2}{C.RESET}  "
                      f"{usage:>5.1f}% {bar}  {label_color(label)}")
            else:
                # Hyperthreading enabled
                print(f"    ├─ Core {core_id} (HT):")
                for logical_id, topo in sorted(logical_cores):
                    stat = stats_map.get(logical_id)
                    usage = stat.usage if stat else 0.0
                    label = stat.label if stat else "N/A"
                    bar = usage_bar(usage, width=15)
                    ht_mark = "⊳" if topo.is_hyperthread else "⊲"
                    
                    print(f"       {ht_mark} {C.CYAN}CPU{logical_id:>2}{C.RESET}  "
                          f"{usage:>5.1f}% {bar}  {label_color(label)}")
    
    # Summary
    total_physical = len(by_physical)
    total_cores = sum(len(set(topo.core_id for _, topo in cores)) for cores in by_physical.values())
    total_logical = len(topology)
    ht_enabled = total_logical > total_cores
    
    print(f"\n  {C.DIM}Summary: {total_physical} physical CPU(s), "
          f"{total_cores} physical core(s), {total_logical} logical core(s)")
    if ht_enabled:
        print(f"  Hyperthreading: {C.GREEN}Enabled{C.RESET}{C.DIM} (⊲ primary, ⊳ sibling){C.RESET}")
    else:
        print(f"  Hyperthreading: {C.DIM}Disabled{C.RESET}")

def print_core_table(core_stats: list[CoreStat]):
    print(f"\n{C.BOLD}{'─'*62}{C.RESET}")
    print(f"{C.BOLD}  CORE TELEMETRY  ({len(core_stats)} logical cores detected){C.RESET}")
    print(f"{C.BOLD}{'─'*62}{C.RESET}")
    print(f"  {'Core':>4}  {'Usage':>6}  {'Bar':<22}  {'Status'}")
    print(f"  {'────':>4}  {'─────':>6}  {'──────────────────────':<22}  {'──────'}")
    for cs in core_stats:
        print(
            f"  {C.DIM}CPU{C.RESET}{cs.core_id:>1}  "
            f"{cs.usage:>5.1f}%  "
            f"{usage_bar(cs.usage):<22}  "
            f"{label_color(cs.label)}"
        )
    hot  = sum(1 for c in core_stats if c.label == "HOT")
    cold = sum(1 for c in core_stats if c.label == "COLD")
    warm = sum(1 for c in core_stats if c.label == "WARM")
    print(f"\n  Summary: {C.RED}●{C.RESET} {hot} Hot  "
          f"{C.YELLOW}●{C.RESET} {warm} Warm  "
          f"{C.CYAN}●{C.RESET} {cold} Cold")

def print_process_table(processes: list[ProcessInfo], show_threads: bool = False):
    print(f"\n{C.BOLD}{'─'*62}{C.RESET}")
    print(f"{C.BOLD}  TOP CPU CONSUMERS{C.RESET}")
    print(f"{C.BOLD}{'─'*62}{C.RESET}")
    print(f"  {'PID':>7}  {'Process':<24}  {'CPU%':>6}  {'Affinity Mask':>14}  Cores")
    print(f"  {'───────':>7}  {'────────────────────────':<24}  {'─────':>6}  {'──────────────':>14}  ─────")
    for p in processes:
        cores_str = ",".join(map(str, p.current_cores)) if p.current_cores else "all"
        print(
            f"  {C.MAGENTA}{p.pid:>7}{C.RESET}  "
            f"{p.name:<24}  "
            f"{C.YELLOW}{p.cpu_percent:>5.1f}%{C.RESET}  "
            f"{C.DIM}{p.affinity_mask:>14}{C.RESET}  "
            f"{cores_str}"
        )
        
        # Show threads if requested and available
        if show_threads and p.threads:
            print(f"  {C.DIM}   └─ Threads ({len(p.threads)}):{C.RESET}")
            for t in p.threads[:10]:  # Show max 10 threads
                t_cores_str = ",".join(map(str, t.current_cores)) if t.current_cores else "all"
                print(
                    f"  {C.DIM}      TID {t.tid:>7}  "
                    f"{t.name:<20}  "
                    f"{t.cpu_percent:>4.1f}%  "
                    f"cores:[{t_cores_str}]{C.RESET}"
                )
            if len(p.threads) > 10:
                print(f"  {C.DIM}      ... {len(p.threads) - 10} more threads{C.RESET}")

def clear_screen():
    """Clear terminal screen."""
    os.system('clear' if os.name != 'nt' else 'cls')

def live_monitor(duration_sec: int = 5, interval_ms: int = 500, filter_pid: Optional[int] = None, show_cpu_freq: bool = False):
    """
    Live monitoring mode - refresh stats every interval for duration seconds.
    If filter_pid is set, only show threads for that specific PID.
    """
    topology = get_cpu_topology()
    num_cores = len(topology)
    
    iterations = int(duration_sec * 1000 / interval_ms)
    
    for i in range(iterations):
        clear_screen()
        print(BANNER)
        
        # Show system info on first iteration
        if i == 0:
            sysinfo = get_system_info(show_cpu_freq=show_cpu_freq)
            print_system_info(sysinfo)
        
        print(f"\n  {C.CYAN}Live Monitor Mode{C.RESET} — {C.DIM}Refresh: {interval_ms}ms  "
              f"Iteration: {i+1}/{iterations}{C.RESET}")
        
        # Get current stats
        core_stats = get_core_usage(sample_ms=interval_ms, topology=topology)
        
        if filter_pid:
            # Show only the specific PID and its threads
            print(f"\n  {C.YELLOW}Filtering: PID {filter_pid}{C.RESET}")
            processes = []
            
            # Try to get this specific process
            rc, out, _ = run(["ps", "-p", str(filter_pid), "-o", "pid,pcpu,comm", "--no-headers"])
            if rc == 0 and out:
                parts = out.strip().split(None, 2)
                if len(parts) >= 3:
                    try:
                        pid = int(parts[0])
                        cpu = float(parts[1])
                        name = parts[2].strip()[:24]
                        
                        rc2, out2, _ = run(["taskset", "-p", str(pid)])
                        affinity_mask = "N/A"
                        cur_cores = []
                        if rc2 == 0:
                            m = re.search(r"current affinity mask:\s*([0-9a-fA-F,]+)", out2)
                            if m:
                                affinity_mask = m.group(1)
                                cur_cores = mask_to_cores(affinity_mask, num_cores)
                        
                        threads = get_threads_for_pid(pid, num_cores)
                        processes.append(ProcessInfo(
                            pid=pid, name=name, cpu_percent=cpu,
                            current_cores=cur_cores, affinity_mask=affinity_mask,
                            threads=threads
                        ))
                    except ValueError:
                        pass
            
            if not processes:
                print(f"  {C.RED}✘ PID {filter_pid} not found or inaccessible{C.RESET}")
            else:
                print_topology(topology, core_stats)
                print_process_table(processes, show_threads=True)
        else:
            # Normal top-N display
            processes = get_top_processes(n=5, num_cores=num_cores, with_threads=False)
            print_topology(topology, core_stats)
            print_process_table(processes, show_threads=False)
        
        print(f"\n  {C.DIM}Press Ctrl+C to exit{C.RESET}")
        
        if i < iterations - 1:
            time.sleep(interval_ms / 1000)
    
    print(f"\n  {C.GREEN}Live monitoring complete.{C.RESET}\n")

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
# Main
# ─────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="TidyCPU — CPU Affinity Optimization Utility",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  sudo python3 tidycpu.py                    # Standard rebalance mode
  sudo python3 tidycpu.py --live             # Live monitor (5 seconds)
  sudo python3 tidycpu.py --live --duration 10  # Live monitor (10 seconds)
  sudo python3 tidycpu.py --pid 1234         # Monitor specific process with threads
  sudo python3 tidycpu.py --pid 1234 --threads  # Show threads in standard mode
  sudo python3 tidycpu.py --cpu-freq         # Include CPU frequency information
        """
    )
    parser.add_argument(
        "--live", "-l",
        action="store_true",
        help="Live monitoring mode (refreshes every 500ms)"
    )
    parser.add_argument(
        "--duration", "-d",
        type=int,
        default=5,
        help="Duration for live mode in seconds (default: 5)"
    )
    parser.add_argument(
        "--pid", "-p",
        type=int,
        help="Filter to show specific PID and its threads"
    )
    parser.add_argument(
        "--threads", "-t",
        action="store_true",
        help="Show thread information for processes"
    )
    parser.add_argument(
        "--cpu-freq", "-f",
        action="store_true",
        help="Show CPU frequency information (min/max/current)"
    )
    
    args = parser.parse_args()
    
    print(BANNER)

    # ── Privilege guard ──────────────────────
    check_root()
    print(f"  {C.GREEN}✔{C.RESET}  Running as root.\n")

    # ── Get system info ──────────────────────
    sysinfo = get_system_info(show_cpu_freq=args.cpu_freq)
    print_system_info(sysinfo)

    # ── Get topology ─────────────────────────
    topology = get_cpu_topology()
    num_cores = len(topology)

    # ── Live monitor mode ────────────────────
    if args.live or args.pid:
        try:
            live_monitor(
                duration_sec=args.duration,
                interval_ms=500,
                filter_pid=args.pid,
                show_cpu_freq=args.cpu_freq
            )
        except KeyboardInterrupt:
            print(f"\n\n  {C.YELLOW}Monitoring interrupted.{C.RESET}\n")
        return

    # ── Standard rebalance mode ──────────────
    # ── 1. Telemetry ─────────────────────────
    print(f"\n  {C.CYAN}○{C.RESET}  Sampling core usage (500 ms) …", end="", flush=True)
    core_stats = get_core_usage(sample_ms=500, topology=topology)
    print(f"\r  {C.GREEN}✔{C.RESET}  Core telemetry collected.          ")

    print_topology(topology, core_stats)
    print_core_table(core_stats)

    # ── 2. Top processes + affinity ───────────
    print(f"\n  {C.CYAN}○{C.RESET}  Identifying top CPU consumers …", end="", flush=True)
    processes = get_top_processes(n=5, num_cores=num_cores, with_threads=args.threads)
    print(f"\r  {C.GREEN}✔{C.RESET}  Process snapshot ready.            ")

    print_process_table(processes, show_threads=args.threads)

    # ── 3. Conflict detection + plan ─────────
    actions = build_rebalance_plan(core_stats, processes)
    print_rebalance_plan(actions)

    if not actions:
        print(f"\n{C.DIM}  Nothing to do. Exiting.{C.RESET}\n")
        sys.exit(0)

    # ── 4. Prompt + execute ──────────────────
    print(f"\n{C.BOLD}  Apply these changes? (y/n): {C.RESET}", end="", flush=True)
    try:
        answer = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        print(f"\n{C.YELLOW}  Aborted.{C.RESET}\n")
        sys.exit(0)

    if answer != "y":
        print(f"  {C.YELLOW}⚡  No changes applied. Plan saved for manual review.{C.RESET}\n")
        # Print manual commands anyway
        for a in actions:
            to_s = cores_to_cpulist(a.to_cores)
            print(f"     {C.DIM}sudo taskset -pc {to_s} {a.pid}{C.RESET}")
        print()
        sys.exit(0)

    print(f"\n  {C.CYAN}○{C.RESET}  Applying affinity changes …\n")
    results = [apply_action(a) for a in actions]
    print_results(results)
    print(f"\n{C.GREEN}{C.BOLD}  TidyCPU complete.{C.RESET}\n")

if __name__ == "__main__":
    main()