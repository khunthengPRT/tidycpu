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
from dataclasses import dataclass, field
from typing import Optional

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
class CoreStat:
    core_id:  int
    usage:    float   # 0.0 – 100.0 percent
    label:    str     # HOT / WARM / COLD
    pids:     list    = field(default_factory=list)

@dataclass
class ProcessInfo:
    pid:          int
    name:         str
    cpu_percent:  float
    current_cores: list[int]
    affinity_mask: str

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

def get_core_usage(sample_ms: int = 500) -> list[CoreStat]:
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

        stats.append(CoreStat(core_id=cid, usage=round(usage, 1), label=label))
    return stats

# ─────────────────────────────────────────────
# Step 2 – Analysis: Top CPU Processes + Affinity
# ─────────────────────────────────────────────
def get_top_processes(n: int = 5, num_cores: int = 1) -> list[ProcessInfo]:
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

        procs.append(ProcessInfo(
            pid=pid, name=name, cpu_percent=cpu,
            current_cores=cur_cores, affinity_mask=affinity_mask
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

def print_process_table(processes: list[ProcessInfo]):
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
    print(BANNER)

    # ── Privilege guard ──────────────────────
    check_root()
    print(f"  {C.GREEN}✔{C.RESET}  Running as root.\n")

    # ── 1. Telemetry ─────────────────────────
    print(f"  {C.CYAN}○{C.RESET}  Sampling core usage (500 ms) …", end="", flush=True)
    core_stats = get_core_usage(sample_ms=500)
    num_cores  = len(core_stats)
    print(f"\r  {C.GREEN}✔{C.RESET}  Core telemetry collected.          ")

    print_core_table(core_stats)

    # ── 2. Top processes + affinity ───────────
    print(f"\n  {C.CYAN}○{C.RESET}  Identifying top CPU consumers …", end="", flush=True)
    processes = get_top_processes(n=5, num_cores=num_cores)
    print(f"\r  {C.GREEN}✔{C.RESET}  Process snapshot ready.            ")

    print_process_table(processes)

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